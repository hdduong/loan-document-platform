from __future__ import annotations

import hashlib
import json
import os
import re
import tomllib
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any
from urllib.parse import unquote

import yaml
from jsonschema import Draft202012Validator, FormatChecker

ROOT = Path(__file__).resolve().parents[1]
IDP_DIR = ROOT / "config" / "idp"
SPEC_KIT_VERSION = "0.12.15"
SPEC_KIT_COMMIT = "7b91c1eda46e1107a53831cd3f14f608b4b7bad0"
GITHUB_REPOSITORY = "hdduong/aws-idp-custom-platform"
PRIVATE_KEY_PEM_PATTERN = re.compile(
    r"-----BEGIN "
    + r"[^\r\n]*"
    + r"PRIVATE KEY-----"
)
SPEC_KIT_SKILLS = {
    "speckit-analyze",
    "speckit-checklist",
    "speckit-clarify",
    "speckit-constitution",
    "speckit-converge",
    "speckit-implement",
    "speckit-plan",
    "speckit-specify",
    "speckit-tasks",
    "speckit-taskstoissues",
}


def repository_files(ignored_roots: set[str]) -> list[Path]:
    files: list[Path] = []
    for directory, subdirectories, filenames in os.walk(ROOT):
        subdirectories[:] = [name for name in subdirectories if name not in ignored_roots]
        base = Path(directory)
        files.extend(base / name for name in filenames)
    return files


def load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def normalized_text_sha256(path: Path) -> str:
    """Hash reviewed text with stable newlines across Git checkout platforms."""

    content = path.read_text(encoding="utf-8")
    normalized = content.replace("\r\n", "\n").replace("\r", "\n")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


class CloudFormationSafeLoader(yaml.SafeLoader):
    """Load CloudFormation YAML tags without constructing executable objects."""


class CloudFormationTaggedValue:
    def __init__(self, tag: str, value: Any) -> None:
        self.tag = tag
        self.value = value


def _construct_cloudformation_value(
    loader: CloudFormationSafeLoader, tag_suffix: str, node: yaml.Node
) -> Any:
    if isinstance(node, yaml.ScalarNode):
        value = loader.construct_scalar(node)
    elif isinstance(node, yaml.SequenceNode):
        value = loader.construct_sequence(node)
    elif isinstance(node, yaml.MappingNode):
        value = loader.construct_mapping(node)
    else:
        raise ValueError(f"Unsupported CloudFormation YAML node: {type(node).__name__}")
    return CloudFormationTaggedValue(tag_suffix, value)


CloudFormationSafeLoader.add_multi_constructor("!", _construct_cloudformation_value)


def _count_nested_scalar(value: Any, expected: str) -> int:
    if isinstance(value, CloudFormationTaggedValue):
        return _count_nested_scalar(value.value, expected)
    if value == expected:
        return 1
    if isinstance(value, dict):
        return sum(
            _count_nested_scalar(key, expected) + _count_nested_scalar(child, expected)
            for key, child in value.items()
        )
    if isinstance(value, list):
        return sum(_count_nested_scalar(child, expected) for child in value)
    return 0


def _inline_role_statements(role: dict[str, Any]) -> list[dict[str, Any]]:
    properties = role.get("Properties")
    policies = properties.get("Policies") if isinstance(properties, dict) else None
    statements: list[dict[str, Any]] = []
    if not isinstance(policies, list):
        return statements
    for policy in policies:
        if not isinstance(policy, dict):
            continue
        policy_document = policy.get("PolicyDocument")
        raw_statements = policy_document.get("Statement") if isinstance(policy_document, dict) else None
        if isinstance(raw_statements, dict):
            raw_statements = [raw_statements]
        if isinstance(raw_statements, list):
            statements.extend(item for item in raw_statements if isinstance(item, dict))
    return statements


def _nested_iam_statements(value: Any) -> list[dict[str, Any]]:
    statements: list[dict[str, Any]] = []
    if isinstance(value, CloudFormationTaggedValue):
        statements.extend(_nested_iam_statements(value.value))
    elif isinstance(value, dict):
        has_action_scope = "Action" in value or "NotAction" in value
        has_resource_scope = "Resource" in value or "NotResource" in value
        if "Effect" in value and has_action_scope and has_resource_scope:
            statements.append(value)
        for child in value.values():
            statements.extend(_nested_iam_statements(child))
    elif isinstance(value, list):
        for child in value:
            statements.extend(_nested_iam_statements(child))
    return statements


def _cloudformation_scalar(value: Any) -> str | None:
    if isinstance(value, str):
        return value
    if (
        isinstance(value, CloudFormationTaggedValue)
        and value.tag == "Sub"
        and isinstance(value.value, str)
    ):
        return value.value
    return None


def _cloudformation_sub_equals(value: Any, expected: str) -> bool:
    return _cloudformation_tagged_equals(value, "Sub", expected)


def _cloudformation_tagged_equals(value: Any, tag: str, expected: Any) -> bool:
    return (
        isinstance(value, CloudFormationTaggedValue)
        and value.tag == tag
        and value.value == expected
    )


def _iam_scope_patterns(
    raw_values: Any, *, allow_substitutions: bool
) -> list[str] | None:
    values = raw_values if isinstance(raw_values, list) else [raw_values]
    if not values:
        return None
    patterns: list[str] = []
    for value in values:
        scalar = _cloudformation_scalar(value)
        if scalar is None:
            return None
        if isinstance(value, CloudFormationTaggedValue):
            if not allow_substitutions:
                return None
            scalar = re.sub(r"\$\{[^}]+\}", "*", scalar)
        patterns.append(scalar)
    return patterns


def _iam_glob_matches(value: str, pattern: str) -> bool:
    expression = re.escape(pattern).replace(r"\*", ".*").replace(r"\?", ".")
    return re.fullmatch(expression, value) is not None


def _resource_pattern_may_cover_transform(pattern: str, symbolic_transform_arn: str) -> bool:
    if _iam_glob_matches(symbolic_transform_arn, pattern):
        return True
    parts = pattern.split(":", 5)
    if len(parts) != 6:
        return False
    arn_prefix, partition, service, region, owner, resource = parts
    variable_component = re.compile(r"^[A-Za-z0-9*?${}._-]+$")
    return (
        _iam_glob_matches("arn", arn_prefix)
        and variable_component.fullmatch(partition) is not None
        and _iam_glob_matches("cloudformation", service)
        and variable_component.fullmatch(region) is not None
        and _iam_glob_matches("aws", owner)
        and _iam_glob_matches("transform/Serverless-2016-10-31", resource)
    )


def _resource_pattern_proves_transform_exclusion(pattern: str) -> bool:
    transform_name = "transform/Serverless-2016-10-31"
    representative_targets = (
        f"arn:aws:cloudformation:us-west-2:aws:{transform_name}",
        f"arn:aws:cloudformation:eu-central-1:aws:{transform_name}",
        f"arn:aws-us-gov:cloudformation:us-gov-west-1:aws:{transform_name}",
        f"arn:aws-cn:cloudformation:cn-north-1:aws:{transform_name}",
        f"arn:aws-iso:cloudformation:us-iso-east-1:aws:{transform_name}",
    )
    return all(_iam_glob_matches(target, pattern) for target in representative_targets)


def _statement_scope_covers_cloudformation_transform(
    statement: dict[str, Any], transform_arn: str
) -> bool | None:
    target_action = "cloudformation:createchangeset"
    uses_action = "Action" in statement
    raw_actions = statement.get("Action", statement.get("NotAction"))
    action_patterns = _iam_scope_patterns(raw_actions, allow_substitutions=uses_action)
    if action_patterns is None:
        return None
    action_pattern_matches = any(
        _iam_glob_matches(target_action, pattern.lower()) for pattern in action_patterns
    )
    action_matches = action_pattern_matches if uses_action else not action_pattern_matches
    if not action_matches:
        return False

    uses_resource = "Resource" in statement
    raw_resources = statement.get("Resource", statement.get("NotResource"))
    resource_patterns = _iam_scope_patterns(raw_resources, allow_substitutions=uses_resource)
    if resource_patterns is None:
        return None
    if uses_resource:
        resource_pattern_matches = any(
            _resource_pattern_may_cover_transform(pattern, transform_arn)
            for pattern in resource_patterns
        )
        resource_matches = resource_pattern_matches
    else:
        provably_excludes_transform = any(
            _resource_pattern_proves_transform_exclusion(pattern)
            for pattern in resource_patterns
        )
        resource_matches = not provably_excludes_transform
    return action_matches and resource_matches


def _statement_action_scope_matches(
    statement: dict[str, Any], target_action: str
) -> bool | None:
    """Return whether an IAM Action/NotAction scope covers one literal action."""

    uses_action = "Action" in statement
    raw_actions = statement.get("Action", statement.get("NotAction"))
    action_patterns = _iam_scope_patterns(raw_actions, allow_substitutions=uses_action)
    if action_patterns is None:
        return None
    pattern_matches = any(
        _iam_glob_matches(target_action.lower(), pattern.lower())
        for pattern in action_patterns
    )
    return pattern_matches if uses_action else not pattern_matches


def validate_serverless_transform_execution_roles(template_text: str) -> None:
    """Require the exact SAM transform permission on both split execution roles."""

    try:
        template = yaml.load(template_text, Loader=CloudFormationSafeLoader)
    except yaml.YAMLError as exc:
        raise ValueError("AWS bootstrap template must be valid CloudFormation YAML.") from exc

    require(isinstance(template, dict), "AWS bootstrap template must be a mapping.")
    resources = template.get("Resources")
    require(isinstance(resources, dict), "AWS bootstrap template must declare Resources.")

    transform_arn = (
        "arn:${AWS::Partition}:cloudformation:${AWS::Region}:aws:transform/Serverless-2016-10-31"
    )
    expected_sid = "ApplyAwsServerlessTransform"
    expected_role_names = (
        "PlatformCloudFormationExecutionRole",
        "IdpCloudFormationExecutionRole",
    )

    prohibited_long_lived_principals = {
        name
        for name, resource in resources.items()
        if isinstance(resource, dict)
        and resource.get("Type") in {"AWS::IAM::User", "AWS::IAM::Group", "AWS::IAM::AccessKey"}
    }
    require(
        not prohibited_long_lived_principals,
        "AWS Serverless transform validation forbids IAM users, groups, and access keys in the OIDC bootstrap.",
    )
    for role_name, role in resources.items():
        if not isinstance(role, dict) or role.get("Type") != "AWS::IAM::Role":
            continue
        properties = role.get("Properties")
        managed_policy_arns = (
            properties.get("ManagedPolicyArns") if isinstance(properties, dict) else None
        )
        require(
            not managed_policy_arns,
            f"{role_name} must not attach an external managed policy that can bypass "
            "AWS Serverless transform validation.",
        )

    require(
        _count_nested_scalar(template, transform_arn) == len(expected_role_names),
        "Only the two split CloudFormation execution roles may reference the AWS Serverless transform.",
    )
    expected_statements: list[dict[str, Any]] = []
    for role_name in expected_role_names:
        role = resources.get(role_name)
        require(
            isinstance(role, dict) and role.get("Type") == "AWS::IAM::Role",
            f"{role_name} must remain an AWS::IAM::Role.",
        )
        statements = _inline_role_statements(role)
        properties = role.get("Properties")
        require(
            isinstance(properties, dict) and not properties.get("PermissionsBoundary"),
            f"{role_name} must not use a permissions boundary that can block the AWS Serverless transform.",
        )
        candidates = [
            statement
            for statement in statements
            if statement.get("Sid") == expected_sid
            or _cloudformation_scalar(statement.get("Resource")) == transform_arn
        ]
        require(
            len(candidates) == 1,
            f"{role_name} must contain exactly one dedicated AWS Serverless transform statement.",
        )
        statement = candidates[0]
        require(
            set(statement) == {"Sid", "Effect", "Action", "Resource"}
            and statement.get("Sid") == expected_sid
            and statement.get("Effect") == "Allow"
            and statement.get("Action") == "cloudformation:CreateChangeSet"
            and _cloudformation_scalar(statement.get("Resource")) == transform_arn,
            f"{role_name} AWS Serverless transform statement must grant only "
            "cloudformation:CreateChangeSet on the exact transform ARN.",
        )
        expected_statements.append(statement)

    iam_statements = _nested_iam_statements(template)
    require(
        all(statement.get("Effect") in ("Allow", "Deny") for statement in iam_statements),
        "AWS Serverless transform validation requires a literal IAM Allow or Deny effect.",
    )
    evaluated_access = [
        (statement, _statement_scope_covers_cloudformation_transform(statement, transform_arn))
        for statement in iam_statements
    ]
    require(
        all(access is not None for _, access in evaluated_access),
        "AWS Serverless transform validation cannot accept an unresolved IAM action or resource scope.",
    )
    transform_access = [
        statement
        for statement, access in evaluated_access
        if statement.get("Effect") == "Allow" and access
    ]
    transform_denials = [
        statement
        for statement, access in evaluated_access
        if statement.get("Effect") == "Deny" and access
    ]
    require(
        not transform_denials,
        "AWS Serverless transform access must not be blocked by an explicit IAM Deny.",
    )
    require(
        len(transform_access) == len(expected_statements)
        and {id(statement) for statement in transform_access}
        == {id(statement) for statement in expected_statements},
        "AWS Serverless transform access must remain exclusive to the two exact execution-role statements.",
    )


def validate_platform_event_source_filters(template_text: str) -> None:
    """Require deployable JSON and the exact upload-completion stream filter."""

    try:
        template = yaml.load(template_text, Loader=CloudFormationSafeLoader)
    except yaml.YAMLError as exc:
        raise ValueError("AWS platform template must be valid CloudFormation YAML.") from exc

    require(isinstance(template, dict), "AWS platform template must be a mapping.")
    resources = template.get("Resources")
    require(isinstance(resources, dict), "AWS platform template must declare Resources.")

    parsed_filters: dict[str, list[dict[str, Any]]] = {}

    def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    def reject_nonstandard_constant(_constant: str) -> None:
        raise ValueError("non-standard JSON constant")

    def parse_filter_criteria(logical_path: str, criteria: Any) -> list[dict[str, Any]]:
        filters = criteria.get("Filters") if isinstance(criteria, dict) else None
        require(
            isinstance(criteria, dict)
            and set(criteria) == {"Filters"}
            and isinstance(filters, list)
            and 1 <= len(filters) <= 5,
            f"{logical_path} FilterCriteria must contain between one and five Filters.",
        )
        parsed: list[dict[str, Any]] = []
        for filter_definition in filters:
            pattern = (
                filter_definition.get("Pattern")
                if isinstance(filter_definition, dict)
                else None
            )
            require(
                isinstance(filter_definition, dict)
                and set(filter_definition) == {"Pattern"}
                and isinstance(pattern, str),
                f"{logical_path} filters must contain one literal Pattern string.",
            )
            try:
                parsed_pattern = json.loads(
                    pattern,
                    object_pairs_hook=reject_duplicate_keys,
                    parse_constant=reject_nonstandard_constant,
                )
            except (json.JSONDecodeError, ValueError) as exc:
                raise ValueError(
                    f"{logical_path} filter Pattern must be valid JSON with unique object keys."
                ) from exc
            require(
                isinstance(parsed_pattern, dict),
                f"{logical_path} filter Pattern must decode to a JSON object.",
            )
            parsed.append(parsed_pattern)
        return parsed

    for logical_id, resource in resources.items():
        if not isinstance(resource, dict):
            continue
        properties = resource.get("Properties")
        if not isinstance(properties, dict):
            continue
        if resource.get("Type") == "AWS::Lambda::EventSourceMapping":
            criteria = properties.get("FilterCriteria")
            if criteria is not None:
                parsed_filters[logical_id] = parse_filter_criteria(logical_id, criteria)
        if resource.get("Type") != "AWS::Serverless::Function" or "Events" not in properties:
            continue
        events = properties.get("Events")
        require(
            isinstance(events, dict),
            f"{logical_id} SAM Events must use literal event definitions.",
        )
        for event_id, event_definition in events.items():
            event_properties = (
                event_definition.get("Properties")
                if isinstance(event_definition, dict)
                else None
            )
            criteria = (
                event_properties.get("FilterCriteria")
                if isinstance(event_properties, dict)
                else None
            )
            if criteria is not None:
                logical_path = f"{logical_id}.Events.{event_id}"
                parsed_filters[logical_path] = parse_filter_criteria(
                    logical_path, criteria
                )

    expected_upload_filter = {
        "eventName": ["INSERT", "MODIFY"],
        "dynamodb": {
            "NewImage": {
                "entityType": {"S": ["UPLOAD"]},
                "status": {"S": ["VALIDATING"]},
            }
        },
    }
    require(
        parsed_filters.get("UploadCompletionStreamMapping") == [expected_upload_filter],
        "UploadCompletionStreamMapping must keep the exact INSERT/MODIFY UPLOAD VALIDATING filter.",
    )


def validate_platform_cloudformation_handler_contract(
    api_template_text: str, bootstrap_template_text: str
) -> None:
    """Keep generated resource names and CloudFormation handler permissions aligned."""

    try:
        api_template = yaml.load(api_template_text, Loader=CloudFormationSafeLoader)
        bootstrap_template = yaml.load(bootstrap_template_text, Loader=CloudFormationSafeLoader)
    except yaml.YAMLError as exc:
        raise ValueError("AWS platform and bootstrap templates must be valid CloudFormation YAML.") from exc

    require(isinstance(api_template, dict), "AWS platform template must be a mapping.")
    require(isinstance(bootstrap_template, dict), "AWS bootstrap template must be a mapping.")
    for template_name, template in (
        ("platform", api_template),
        ("bootstrap", bootstrap_template),
    ):
        parameters = template.get("Parameters")
        environment_name = (
            parameters.get("EnvironmentName") if isinstance(parameters, dict) else None
        )
        require(
            isinstance(environment_name, dict)
            and environment_name.get("Type") == "String"
            and environment_name.get("AllowedPattern") == "[a-z0-9-]+"
            and environment_name.get("MaxLength") == 13,
            f"AWS {template_name} EnvironmentName must keep deterministic S3 names valid.",
        )

    api_resources = api_template.get("Resources")
    require(isinstance(api_resources, dict), "AWS platform template must declare Resources.")

    def resource_tag_value(resource: Any, key: str) -> Any:
        properties = resource.get("Properties") if isinstance(resource, dict) else None
        tags = properties.get("Tags") if isinstance(properties, dict) else None
        if not isinstance(tags, list):
            return None
        matches = [
            tag.get("Value")
            for tag in tags
            if isinstance(tag, dict) and tag.get("Key") == key
        ]
        return matches[0] if len(matches) == 1 else None

    data_key = api_resources.get("DataKey")
    require(
        isinstance(data_key, dict)
        and data_key.get("Type") == "AWS::KMS::Key"
        and resource_tag_value(data_key, "KeyPurpose") == "document-data",
        "Platform DataKey must carry the exact document-data KeyPurpose tag.",
    )
    resource_name_contracts = (
        (
            "RegistryTable",
            "AWS::DynamoDB::Table",
            "TableName",
            "loan-document-${EnvironmentName}-registry",
        ),
        (
            "SourceBucket",
            "AWS::S3::Bucket",
            "BucketName",
            "loan-document-${EnvironmentName}-source-${AWS::AccountId}-${AWS::Region}",
        ),
    )
    for logical_id, resource_type, property_name, expected_name in resource_name_contracts:
        resource = api_resources.get(logical_id)
        properties = resource.get("Properties") if isinstance(resource, dict) else None
        require(
            isinstance(resource, dict)
            and resource.get("Type") == resource_type
            and isinstance(properties, dict)
            and _cloudformation_sub_equals(properties.get(property_name), expected_name),
            f"{logical_id} must use the deterministic name authorized for its "
            "CloudFormation handler.",
        )

    bootstrap_resources = bootstrap_template.get("Resources")
    require(isinstance(bootstrap_resources, dict), "AWS bootstrap template must declare Resources.")
    artifact_bucket = bootstrap_resources.get("ArtifactBucket")
    artifact_bucket_properties = (
        artifact_bucket.get("Properties") if isinstance(artifact_bucket, dict) else None
    )
    require(
        isinstance(artifact_bucket, dict)
        and artifact_bucket.get("Type") == "AWS::S3::Bucket"
        and isinstance(artifact_bucket_properties, dict)
        and _cloudformation_sub_equals(
            artifact_bucket_properties.get("BucketName"),
            "loan-document-${EnvironmentName}-ci-artifacts-${AWS::AccountId}-${AWS::Region}",
        ),
        "ArtifactBucket must keep the deterministic name used by deployment artifact access.",
    )
    artifact_bucket_encryption = artifact_bucket_properties.get("BucketEncryption")
    artifact_bucket_encryption_rules = (
        artifact_bucket_encryption.get("ServerSideEncryptionConfiguration")
        if isinstance(artifact_bucket_encryption, dict)
        else None
    )
    artifact_bucket_encryption_rule = (
        artifact_bucket_encryption_rules[0]
        if isinstance(artifact_bucket_encryption_rules, list)
        and len(artifact_bucket_encryption_rules) == 1
        else None
    )
    artifact_bucket_encryption_default = (
        artifact_bucket_encryption_rule.get("ServerSideEncryptionByDefault")
        if isinstance(artifact_bucket_encryption_rule, dict)
        else None
    )
    require(
        isinstance(artifact_bucket_encryption, dict)
        and set(artifact_bucket_encryption) == {"ServerSideEncryptionConfiguration"}
        and isinstance(artifact_bucket_encryption_rule, dict)
        and set(artifact_bucket_encryption_rule)
        == {"BucketKeyEnabled", "ServerSideEncryptionByDefault"}
        and artifact_bucket_encryption_rule.get("BucketKeyEnabled") is True
        and isinstance(artifact_bucket_encryption_default, dict)
        and set(artifact_bucket_encryption_default)
        == {"SSEAlgorithm", "KMSMasterKeyID"}
        and artifact_bucket_encryption_default.get("SSEAlgorithm") == "aws:kms"
        and _cloudformation_tagged_equals(
            artifact_bucket_encryption_default.get("KMSMasterKeyID"),
            "GetAtt",
            "ArtifactKey.Arn",
        ),
        "ArtifactBucket must use ArtifactKey with S3 Bucket Keys for its reviewed encryption context.",
    )
    artifact_key = bootstrap_resources.get("ArtifactKey")
    require(
        isinstance(artifact_key, dict)
        and artifact_key.get("Type") == "AWS::KMS::Key"
        and resource_tag_value(artifact_key, "KeyPurpose") == "deployment-artifacts",
        "ArtifactKey must carry the exact deployment-artifacts KeyPurpose tag.",
    )
    platform_role = bootstrap_resources.get("PlatformCloudFormationExecutionRole")
    platform_properties = (
        platform_role.get("Properties") if isinstance(platform_role, dict) else None
    )
    platform_metadata = (
        platform_role.get("Metadata") if isinstance(platform_role, dict) else None
    )
    cfn_lint_metadata = (
        platform_metadata.get("cfn-lint")
        if isinstance(platform_metadata, dict)
        else None
    )
    cfn_lint_config = (
        cfn_lint_metadata.get("config")
        if isinstance(cfn_lint_metadata, dict)
        else None
    )
    require(
        isinstance(platform_role, dict)
        and platform_role.get("Type") == "AWS::IAM::Role"
        and isinstance(platform_properties, dict)
        and not platform_properties.get("ManagedPolicyArns"),
        "Platform CloudFormation execution role must remain an inline-policy IAM role.",
    )
    require(
        isinstance(cfn_lint_config, dict)
        and cfn_lint_config.get("ignore_checks") == ["W3037"],
        "Platform CloudFormation may suppress only the stale W3037 Backup mount catalog warning.",
    )
    platform_statements = _inline_role_statements(platform_role)

    standalone_role_attachments = []
    for logical_id, resource in bootstrap_resources.items():
        if not isinstance(resource, dict) or resource.get("Type") not in {
            "AWS::IAM::Policy",
            "AWS::IAM::ManagedPolicy",
        }:
            continue
        properties = resource.get("Properties")
        roles = properties.get("Roles") if isinstance(properties, dict) else None
        if roles:
            standalone_role_attachments.append(logical_id)
    require(
        not standalone_role_attachments,
        "Bootstrap IAM policies must not bypass reviewed inline role definitions with Roles attachments.",
    )

    def action_candidates(target_actions: set[str], reviewed_sids: set[str]) -> list[dict[str, Any]]:
        candidates: list[dict[str, Any]] = []
        for statement in platform_statements:
            matches = [
                _statement_action_scope_matches(statement, target_action)
                for target_action in target_actions
            ]
            require(
                all(match is not None for match in matches),
                "Platform CloudFormation handler IAM actions must be statically reviewable.",
            )
            if statement.get("Sid") in reviewed_sids or any(matches):
                candidates.append(statement)
        return candidates

    def action_scope_targets_services(
        statement: dict[str, Any], services: set[str]
    ) -> bool:
        require(
            statement.get("Effect") in {"Allow", "Deny"},
            "Platform CloudFormation handler IAM effects must be statically reviewable.",
        )
        if "NotAction" in statement:
            return True
        action_patterns = _iam_scope_patterns(
            statement.get("Action"), allow_substitutions=True
        )
        require(
            action_patterns is not None,
            "Platform CloudFormation handler IAM actions must be statically reviewable.",
        )
        for pattern in action_patterns:
            if pattern == "*":
                return True
            parts = pattern.lower().split(":", 1)
            if len(parts) == 2 and any(
                _iam_glob_matches(service, parts[0]) for service in services
            ):
                return True
        return False

    def resource_pattern_targets_services(pattern: str, services: set[str]) -> bool:
        if pattern == "*":
            return True
        parts = pattern.split(":", 5)
        return len(parts) == 6 and any(
            _iam_glob_matches(service, parts[2].lower()) for service in services
        )

    expected_resource_arns = {
        "arn:${AWS::Partition}:dynamodb:${AWS::Region}:${AWS::AccountId}:"
        "table/loan-document-${EnvironmentName}-registry",
        "arn:${AWS::Partition}:dynamodb:${AWS::Region}:${AWS::AccountId}:"
        "table/loan-document-${EnvironmentName}-registry/*",
        "arn:${AWS::Partition}:s3:::loan-document-${EnvironmentName}-source-"
        "${AWS::AccountId}-${AWS::Region}",
        "arn:${AWS::Partition}:s3:::loan-document-${EnvironmentName}-source-"
        "${AWS::AccountId}-${AWS::Region}/*",
    }
    identity_statements = action_candidates(
        {"dynamodb:DescribeTable", "s3:CreateBucket"},
        {"ExactPlatformResources"},
    )
    identity_statement = identity_statements[0] if len(identity_statements) == 1 else {}
    identity_actions = identity_statement.get("Action")
    identity_resources = identity_statement.get("Resource")
    identity_actions = identity_actions if isinstance(identity_actions, list) else []
    identity_resources = identity_resources if isinstance(identity_resources, list) else []
    identity_resource_patterns = [
        _cloudformation_scalar(value) for value in identity_resources
    ]
    relevant_identity_resources = [
        value
        for value in identity_resources
        if (_cloudformation_scalar(value) or "").startswith(
            (
                "arn:${AWS::Partition}:dynamodb:",
                "arn:${AWS::Partition}:s3:::",
            )
        )
    ]
    relevant_identity_grants = [
        statement
        for statement in platform_statements
        if action_scope_targets_services(statement, {"dynamodb", "s3"})
    ]
    artifact_read_statements = [
        statement
        for statement in platform_statements
        if statement.get("Sid") == "ReadPlatformDeploymentArtifacts"
    ]
    artifact_read_statement = (
        artifact_read_statements[0] if len(artifact_read_statements) == 1 else {}
    )
    require(
        len(artifact_read_statements) == 1
        and set(artifact_read_statement) == {"Sid", "Effect", "Action", "Resource"}
        and artifact_read_statement.get("Effect") == "Allow"
        and artifact_read_statement.get("Action") == "s3:GetObject"
        and _cloudformation_sub_equals(
            artifact_read_statement.get("Resource"),
            "${ArtifactBucket.Arn}/platform/${EnvironmentName}/*",
        ),
        "Platform CloudFormation must read only its environment deployment-artifact prefix.",
    )
    require(
        len(identity_statements) == 1
        and len(relevant_identity_grants) == 2
        and {id(statement) for statement in relevant_identity_grants}
        == {id(identity_statement), id(artifact_read_statement)}
        and set(identity_statement) == {"Sid", "Effect", "Action", "Resource"}
        and identity_statement.get("Sid") == "ExactPlatformResources"
        and identity_statement.get("Effect") == "Allow"
        and {
            action
            for action in identity_actions
            if isinstance(action, str)
            and (
                _iam_glob_matches("dynamodb:describetable", action.lower())
                or _iam_glob_matches("s3:createbucket", action.lower())
            )
        }
        == {"dynamodb:*", "s3:*"}
        and all(pattern is not None for pattern in identity_resource_patterns)
        and all(
            not resource_pattern_targets_services(pattern, {"dynamodb", "s3"})
            or pattern in expected_resource_arns
            for pattern in identity_resource_patterns
            if pattern is not None
        )
        and len(relevant_identity_resources) == len(expected_resource_arns)
        and all(
            isinstance(value, CloudFormationTaggedValue) and value.tag == "Sub"
            for value in relevant_identity_resources
        )
        and {
            _cloudformation_scalar(value) for value in relevant_identity_resources
        }
        == expected_resource_arns,
        "Platform CloudFormation resource names must match the exact authorized table and bucket ARNs.",
    )

    artifact_decrypt_statements = [
        statement
        for statement in platform_statements
        if statement.get("Sid") == "DecryptPlatformDeploymentArtifacts"
        or _cloudformation_tagged_equals(
            statement.get("Resource"), "GetAtt", "ArtifactKey.Arn"
        )
    ]
    artifact_decrypt_statement = (
        artifact_decrypt_statements[0]
        if len(artifact_decrypt_statements) == 1
        else {}
    )
    artifact_decrypt_condition = artifact_decrypt_statement.get("Condition")
    artifact_decrypt_via_service = (
        artifact_decrypt_condition.get("StringEquals")
        if isinstance(artifact_decrypt_condition, dict)
        else None
    )
    artifact_decrypt_context = (
        artifact_decrypt_via_service.get("kms:EncryptionContext:aws:s3:arn")
        if isinstance(artifact_decrypt_via_service, dict)
        else None
    )
    require(
        len(artifact_decrypt_statements) == 1
        and set(artifact_decrypt_statement)
        == {"Sid", "Effect", "Action", "Resource", "Condition"}
        and artifact_decrypt_statement.get("Sid")
        == "DecryptPlatformDeploymentArtifacts"
        and artifact_decrypt_statement.get("Effect") == "Allow"
        and artifact_decrypt_statement.get("Action") == "kms:Decrypt"
        and _cloudformation_tagged_equals(
            artifact_decrypt_statement.get("Resource"),
            "GetAtt",
            "ArtifactKey.Arn",
        )
        and isinstance(artifact_decrypt_condition, dict)
        and set(artifact_decrypt_condition) == {"StringEquals"}
        and isinstance(artifact_decrypt_via_service, dict)
        and set(artifact_decrypt_via_service)
        == {"kms:ViaService", "kms:EncryptionContext:aws:s3:arn"}
        and _cloudformation_sub_equals(
            artifact_decrypt_via_service.get("kms:ViaService"),
            "s3.${AWS::Region}.${AWS::URLSuffix}",
        )
        and _cloudformation_tagged_equals(
            artifact_decrypt_context,
            "GetAtt",
            "ArtifactBucket.Arn",
        ),
        "Platform CloudFormation must decrypt only the exact deployment ArtifactKey.",
    )

    def statement_by_sid(sid: str) -> dict[str, Any]:
        matches = [
            statement for statement in platform_statements if statement.get("Sid") == sid
        ]
        require(len(matches) == 1, f"Platform CloudFormation requires one exact {sid} statement.")
        return matches[0]

    create_data_key_statement = statement_by_sid("CreateTaggedDataKey")
    create_data_key_condition = create_data_key_statement.get("Condition")
    create_data_key_tags = (
        create_data_key_condition.get("StringEquals")
        if isinstance(create_data_key_condition, dict)
        else None
    )
    require(
        set(create_data_key_statement) == {"Sid", "Effect", "Action", "Resource", "Condition"}
        and create_data_key_statement.get("Effect") == "Allow"
        and create_data_key_statement.get("Action") == "kms:CreateKey"
        and create_data_key_statement.get("Resource") == "*"
        and isinstance(create_data_key_tags, dict)
        and set(create_data_key_tags)
        == {
            "aws:RequestTag/Application",
            "aws:RequestTag/Environment",
            "aws:RequestTag/KeyPurpose",
        }
        and create_data_key_tags.get("aws:RequestTag/Application")
        == "loan-document-platform"
        and _cloudformation_tagged_equals(
            create_data_key_tags.get("aws:RequestTag/Environment"),
            "Ref",
            "EnvironmentName",
        )
        and create_data_key_tags.get("aws:RequestTag/KeyPurpose") == "document-data",
        "Platform CloudFormation may create only purpose-tagged document data keys.",
    )

    manage_data_key_statement = statement_by_sid("ManageTaggedDataKeys")
    manage_data_key_condition = manage_data_key_statement.get("Condition")
    manage_data_key_tags = (
        manage_data_key_condition.get("StringEquals")
        if isinstance(manage_data_key_condition, dict)
        else None
    )
    require(
        set(manage_data_key_statement) == {"Sid", "Effect", "Action", "Resource", "Condition"}
        and manage_data_key_statement.get("Effect") == "Allow"
        and manage_data_key_statement.get("Action") == "kms:*"
        and _cloudformation_sub_equals(
            manage_data_key_statement.get("Resource"),
            "arn:${AWS::Partition}:kms:${AWS::Region}:${AWS::AccountId}:key/*",
        )
        and isinstance(manage_data_key_tags, dict)
        and set(manage_data_key_tags)
        == {
            "aws:ResourceTag/Application",
            "aws:ResourceTag/Environment",
            "aws:ResourceTag/KeyPurpose",
        }
        and manage_data_key_tags.get("aws:ResourceTag/Application")
        == "loan-document-platform"
        and _cloudformation_tagged_equals(
            manage_data_key_tags.get("aws:ResourceTag/Environment"),
            "Ref",
            "EnvironmentName",
        )
        and manage_data_key_tags.get("aws:ResourceTag/KeyPurpose") == "document-data",
        "Platform CloudFormation data-key management must exclude deployment artifact keys.",
    )
    kms_decrypt_statements = action_candidates(
        {"kms:Decrypt"},
        {"ManageTaggedDataKeys", "DecryptPlatformDeploymentArtifacts"},
    )
    require(
        len(kms_decrypt_statements) == 2
        and {id(statement) for statement in kms_decrypt_statements}
        == {id(manage_data_key_statement), id(artifact_decrypt_statement)},
        "Only the reviewed purpose-tagged data-key and artifact decrypt grants may cover kms:Decrypt.",
    )

    expected_mount_actions = {
        "backup-storage:Mount",
        "backup-storage:MountCapsule",
    }
    mount_statements = action_candidates(
        {"backup-storage:Mount", "backup-storage:MountCapsule"},
        {"MountEncryptedBackupVault"},
    )
    require(
        len(mount_statements) == 1
        and set(mount_statements[0]) == {"Sid", "Effect", "Action", "Resource"}
        and mount_statements[0].get("Sid") == "MountEncryptedBackupVault"
        and mount_statements[0].get("Effect") == "Allow"
        and mount_statements[0].get("Resource") == "*"
        and isinstance(mount_statements[0].get("Action"), list)
        and set(mount_statements[0]["Action"]) == expected_mount_actions,
        "Platform CloudFormation must allow only the reviewed Backup vault handler actions.",
    )

    backup_role_arn = (
        "arn:${AWS::Partition}:iam::${AWS::AccountId}:role/aws-service-role/"
        "backup.amazonaws.com/AWSServiceRoleForBackup"
    )
    expected_service_linked_role_sids = {
        "GuardDutyServiceLinkedRole",
        "BackupServiceLinkedRole",
    }
    service_linked_role_statements = action_candidates(
        {"iam:CreateServiceLinkedRole"},
        expected_service_linked_role_sids,
    )
    service_linked_role_by_sid = {
        statement.get("Sid"): statement for statement in service_linked_role_statements
    }
    guardduty_statement = service_linked_role_by_sid.get("GuardDutyServiceLinkedRole")
    backup_statement = service_linked_role_by_sid.get("BackupServiceLinkedRole")
    require(
        len(service_linked_role_statements) == len(expected_service_linked_role_sids)
        and set(service_linked_role_by_sid) == expected_service_linked_role_sids
        and guardduty_statement
        == {
            "Sid": "GuardDutyServiceLinkedRole",
            "Effect": "Allow",
            "Action": "iam:CreateServiceLinkedRole",
            "Resource": "*",
            "Condition": {
                "StringEquals": {"iam:AWSServiceName": "guardduty.amazonaws.com"}
            },
        }
        and isinstance(backup_statement, dict)
        and set(backup_statement) == {"Sid", "Effect", "Action", "Resource", "Condition"}
        and backup_statement.get("Sid") == "BackupServiceLinkedRole"
        and backup_statement.get("Effect") == "Allow"
        and backup_statement.get("Action") == "iam:CreateServiceLinkedRole"
        and _cloudformation_sub_equals(backup_statement.get("Resource"), backup_role_arn)
        and backup_statement.get("Condition")
        == {"StringEquals": {"iam:AWSServiceName": "backup.amazonaws.com"}},
        "Platform CloudFormation may create only the exact approved service-linked roles.",
    )


def validate_platform_packaging_contract(script_text: str) -> None:
    """Keep SAM's artifact producer coordinates aligned with execution-role IAM."""

    required_fragments = (
        "'--s3-bucket', [string]$bootstrap.ArtifactBucketName",
        "'--s3-prefix', \"platform/$($config.environment)\"",
        "'--kms-key-id', [string]$bootstrap.ArtifactKeyArn",
    )
    for fragment in required_fragments:
        require(
            script_text.count(fragment) == 1,
            "Platform SAM packaging must use the exact reviewed artifact bucket, "
            "environment prefix, and encryption key.",
        )


def _setup_python_versions(workflow_text: str, workflow_name: str) -> list[str]:
    workflow = yaml.load(workflow_text, Loader=yaml.BaseLoader)
    require(isinstance(workflow, dict), f"Invalid GitHub workflow: {workflow_name}")
    jobs = workflow.get("jobs")
    require(isinstance(jobs, dict), f"GitHub workflow jobs must be a mapping: {workflow_name}")
    versions: list[str] = []
    for job in jobs.values():
        require(isinstance(job, dict), f"GitHub workflow job must be a mapping: {workflow_name}")
        steps = job.get("steps", [])
        require(isinstance(steps, list), f"GitHub workflow steps must be a list: {workflow_name}")
        for step in steps:
            if not isinstance(step, dict) or not str(step.get("uses", "")).startswith(
                "actions/setup-python@"
            ):
                continue
            configuration = step.get("with")
            require(
                isinstance(configuration, dict) and isinstance(configuration.get("python-version"), str),
                f"setup-python must declare python-version: {workflow_name}",
            )
            versions.append(configuration["python-version"])
    return versions


def validate_idp_python_toolchain_contract(
    lock: dict[str, Any],
    deploy_script: str,
    bootstrap_script: str,
    production_workflow: str,
    validation_workflow: str,
) -> None:
    """Keep the pinned IDP CLI on its supported minor without moving platform code."""

    require(
        lock.get("cliPythonVersion") == "3.12",
        "Pinned IDP 0.5.16 CLI must use Python 3.12.",
    )
    require(
        lock.get("cliBuildTools")
        == {"cfnLint": "1.53.1", "ruff": "0.15.22", "uv": "0.9.6"},
        "Pinned IDP publisher build-tool versions changed.",
    )
    for fragment in (
        "Resolve-PythonLaunch -Version ([string]$lock.cliPythonVersion)",
        '".local/tools/idp-cli-$($lock.version)-py$pythonRuntimeTag"',
        "lib/idp_common_pkg')[all]",
        "'-m', 'pip', 'check'",
        'm.version(\"numpy\") == \"1.26.4\"',
        '"cfn-lint==$($lock.cliBuildTools.cfnLint)"',
        '"ruff==$($lock.cliBuildTools.ruff)"',
        '"uv==$($lock.cliBuildTools.uv)"',
        "'--force-reinstall', '--no-deps'",
        "|tools=$buildToolIdentity",
        "$buildToolExecutables = @(",
        "($bridgeExecutables + $buildToolExecutables)",
        "foreach ($requiredExecutable in ($bridgeExecutables + $buildToolExecutables))",
        "m.version('cfn-lint')",
        "m.version('ruff')",
        "m.version('uv')",
        "Invoke-Checked -Command ruff -Arguments @('--version')",
        "Invoke-Checked -Command cfn-lint -Arguments @('--version')",
        "Invoke-Checked -Command uv -Arguments @('--version')",
        "Invoke-WithPrependedPath -Path $venvExecutableDirectory -Environment $cliEnvironment",
    ):
        require(fragment in deploy_script, f"Pinned IDP Python gate lacks: {fragment}")
    legacy_command_fragment = "foreach ($command in 'aws', 'git', 'sam', 'docker', 'node', 'npm')"
    github_image_command_fragment = "foreach ($command in 'aws', 'git', 'sam', 'node', 'npm')"
    if "ImageManifestFile" in deploy_script:
        require(legacy_command_fragment not in deploy_script, "IDP deployment must not require Docker for GitHub-built images.")
        require(
            github_image_command_fragment in deploy_script,
            "Manifest-driven IDP deployment lacks its command preflight.",
        )
    else:
        require(
            github_image_command_fragment in deploy_script or legacy_command_fragment in deploy_script,
            "Pinned IDP Python gate lacks its command preflight.",
        )
    require(
        "foreach ($command in 'aws', 'git', 'python'," not in deploy_script,
        "IDP deployment must not require generic Python before exact-minor resolution.",
    )
    require(
        deploy_script.count("'--force-reinstall', '--no-deps'") == 2,
        "IDP repair must force-reinstall both publisher tools and the Windows bridge.",
    )
    for fragment in (
        "Python.Python.3.13",
        "Python.Python.3.12",
        "Resolve-PythonLaunch -Version $requiredIdpPythonVersion",
        "--source', 'winget'",
        "--source winget",
    ):
        require(fragment in bootstrap_script, f"Bootstrap Python split lacks: {fragment}")
    require(
        _setup_python_versions(production_workflow, "deploy-prod.yml") == ["3.12", "3.13"],
        "Production must set up IDP Python 3.12 before restoring platform Python 3.13.",
    )
    require(
        _setup_python_versions(validation_workflow, "validate.yml") == ["3.13"],
        "Pull-request validation must remain on platform Python 3.13 only.",
    )
    for executable in ("ruff", "cfn-lint", "uv"):
        require(
            deploy_script.index(f"Invoke-Checked -Command {executable} -Arguments @('--version')")
            < deploy_script.index("[IO.File]::WriteAllText($installMarker"),
            f"Pinned publisher tool {executable} must pass before the cache marker is written.",
        )


def validate_idp_windows_bridge_contract(
    deploy_script: str,
    common_module: str,
    bridge_project: str,
    bridge_source: str,
) -> None:
    """Keep Windows child-tool relays native, scoped, reviewable, and cache-bound."""

    for fragment in (
        "Resolve-CommandSourceOutsidePath -Name sam",
        "Resolve-CommandSourceOutsidePath -Name node",
        "Resolve-CommandSourceOutsidePath -Name npm",
        "$bridgeSources = @(",
        "$bridgeIdentity = ($bridgeSources | ForEach-Object { Get-NormalizedTextSha256 -Path $_ })",
        "$expectedInstallIdentity =",
        "'--no-deps',",
        "'--editable', $bridgePackageDirectory",
        "$cliEnvironment = @{ PYTHONUTF8 = '1' }",
        "IDP_SAM_NATIVE_EXECUTABLE",
        "IDP_SAM_CLI_PYTHON",
        "IDP_NPM_NATIVE_EXECUTABLE",
        "IDP_NODE_EXECUTABLE",
        "IDP_NPM_CLI_JS",
        "$cliEnvironment[$entry.Name] = [string]$entry.Value",
        "Invoke-WithPrependedPath -Path $venvExecutableDirectory -Environment $cliEnvironment",
        "Invoke-Checked -Command sam -Arguments @('--version')",
        "Invoke-Checked -Command npm -Arguments @('--version')",
    ):
        require(fragment in deploy_script, f"Windows IDP bridge deployment lacks: {fragment}")
    require(
        ".BridgeRequired" not in deploy_script,
        "Every Windows topology must install and hash the reviewed native bridge.",
    )
    require(
        "IsNullOrWhiteSpace([string]$entry.Value)" not in deploy_script,
        "Windows bridge target variables must be explicitly cleared when inapplicable.",
    )
    require(
        deploy_script.index("Invoke-Checked -Command sam -Arguments @('--version')")
        < deploy_script.index("[IO.File]::WriteAllText($installMarker"),
        "The Windows bridge must pass child-tool smoke tests before its cache marker is written.",
    )

    for fragment in (
        "function Resolve-CommandSourceOutsidePath",
        "-CommandType Application -All",
        "function Resolve-WindowsIdpCliBridge",
        "%~dp0/",
        "../runtime/python.exe",
        "Lib/site-packages/samcli",
        "node_modules/npm/bin/npm-cli.js",
        "must share an installation directory",
        'Remove-Item -LiteralPath "Env:$name"',
    ):
        require(fragment in common_module, f"Windows IDP bridge resolver lacks: {fragment}")

    for fragment in (
        'requires = ["setuptools==80.9.0"]',
        'requires-python = ">=3.12,<3.13"',
        'sam = "idp_windows_cli_bridge:main"',
        'npm = "idp_windows_cli_bridge:main"',
    ):
        require(fragment in bridge_project, f"Windows IDP bridge package lacks: {fragment}")

    for prohibited in ("shell=True", "shell = True", "os.system", "cmd.exe", "cmd /c"):
        require(prohibited not in bridge_source, f"Windows IDP bridge uses a shell path: {prohibited}")
    for fragment in (
        "not path.is_absolute()",
        'if launcher_path.suffix.casefold() != ".exe"',
        'candidates.append(Path(f"{launcher}.exe"))',
        "if _targets_launcher(command[0], sys.argv[0])",
        'subprocess.run(command, check=False, shell=False)',
        'return [python, "-m", "samcli", *arguments]',
        "return [node, npm_cli, *arguments]",
    ):
        require(fragment in bridge_source, f"Windows IDP bridge relay lacks: {fragment}")


def validate_workflow_actions(value: Any, path: Path) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if key == "uses" and isinstance(child, str) and not child.startswith("./"):
                require(
                    re.search(r"@[0-9a-f]{40}$", child) is not None,
                    f"Workflow action must be pinned to an immutable commit SHA in {path}: {child}",
                )
            validate_workflow_actions(child, path)
    elif isinstance(value, list):
        for child in value:
            validate_workflow_actions(child, path)


def workflow_trigger_names(value: Any, path: Path) -> set[str]:
    if isinstance(value, str):
        return {value}
    if isinstance(value, list):
        require(all(isinstance(item, str) for item in value), f"Invalid workflow trigger list: {path}")
        return set(value)
    if isinstance(value, dict):
        require(all(isinstance(item, str) for item in value), f"Invalid workflow trigger map: {path}")
        return set(value)
    raise ValueError(f"Workflow must declare an on trigger: {path}")


def resolve_repository_path(base: Path, target: str, label: str) -> Path:
    require(not PurePosixPath(target).is_absolute(), f"Absolute {label}: {target}")
    require(not PureWindowsPath(target).is_absolute(), f"Absolute {label}: {target}")
    resolved = (base / target).resolve()
    try:
        resolved.relative_to(ROOT.resolve())
    except ValueError as error:
        raise ValueError(f"{label.capitalize()} escapes the repository: {target}") from error
    return resolved


def validate_copilot_review_gate() -> None:
    path = ROOT / ".github" / "workflows" / "copilot-review.yml"
    with path.open("r", encoding="utf-8") as handle:
        workflow = yaml.load(handle, Loader=yaml.BaseLoader)
    require(isinstance(workflow, dict), f"Invalid Copilot review workflow: {path}")
    triggers = workflow.get("on")
    require(isinstance(triggers, dict), "Copilot review gate must declare explicit triggers.")
    trigger_names = workflow_trigger_names(triggers, path)
    require(trigger_names == {"pull_request"}, "Copilot review gate must run only for pull requests.")
    pull_request = triggers["pull_request"]
    require(isinstance(pull_request, dict), "Copilot pull_request trigger must specify event types.")
    require(
        set(pull_request.get("types", [])) == {"opened", "reopened", "synchronize", "ready_for_review"},
        "Copilot review gate trigger types changed.",
    )

    permissions = workflow.get("permissions")
    require(
        permissions == {"contents": "read", "pull-requests": "read"},
        "Copilot review gate must remain metadata-only and read-only.",
    )
    jobs = workflow.get("jobs")
    require(isinstance(jobs, dict) and set(jobs) == {"copilot-review"}, "Unexpected Copilot jobs.")
    job = jobs["copilot-review"]
    require(job.get("timeout-minutes") == "20", "Copilot review gate timeout changed.")
    steps = job.get("steps")
    require(isinstance(steps, list) and len(steps) == 1, "Copilot review gate must have one metadata step.")
    step = steps[0]
    require("uses" not in step, "Copilot review gate must not execute a third-party action or checkout code.")
    script = step.get("run", "")
    for required_fragment in (
        "copilot-pull-request-reviewer[bot]",
        ".state == \\\"COMMENTED\\\"",
        ".commit_id == \\\"${HEAD_SHA}\\\"",
        "pulls/${PR_NUMBER}/reviews",
        "exit 1",
    ):
        require(required_fragment in script, f"Copilot exact-head gate is missing: {required_fragment}")


def validate_python_quality_gate() -> None:
    requirements = (ROOT / "requirements-dev.txt").read_text(encoding="utf-8").splitlines()
    require("coverage==7.15.2" in requirements, "Coverage.py must remain pinned.")
    require("pytest-cov==7.1.0" in requirements, "pytest-cov must remain pinned.")

    with (ROOT / "pyproject.toml").open("rb") as handle:
        project = tomllib.load(handle)
    coverage = project.get("tool", {}).get("coverage", {})
    run = coverage.get("run", {})
    require(run.get("branch") is True, "Python coverage must collect branch data.")
    require(run.get("source") == ["services", "tooling"], "Python coverage must include every service and tooling module.")

    checker = (ROOT / "scripts" / "check-python-coverage.py").read_text(encoding="utf-8")
    require("MINIMUM_LINE_COVERAGE = 80.0" in checker, "Python per-file coverage floor changed.")
    require("PRODUCTION_ROOTS = (PurePosixPath(\"services\"), PurePosixPath(\"tooling\"))" in checker, "Python coverage scope changed.")

    for workflow_name in ("validate.yml", "deploy-prod.yml"):
        workflow = (ROOT / ".github" / "workflows" / workflow_name).read_text(encoding="utf-8")
        for fragment in (
            "--cov=services",
            "--cov-branch",
            "--cov-report=json:coverage.json",
            "python scripts/check-python-coverage.py coverage.json",
        ):
            require(fragment in workflow, f"{workflow_name} does not enforce Python coverage: {fragment}")


def validate_web_quality_gate() -> None:
    web = ROOT / "apps" / "web"
    package_path = web / "package.json"
    authored_source = any(
        path.is_file()
        for directory in (web / "src", web / "e2e")
        if directory.exists()
        for path in directory.rglob("*")
    )
    require(not authored_source or package_path.is_file(), "React source exists without apps/web/package.json.")

    validate_workflow = (ROOT / ".github" / "workflows" / "validate.yml").read_text(encoding="utf-8")
    for fragment in (
        "npm audit --audit-level=high",
        "npm run typecheck",
        "npm run test:coverage",
        "npx playwright install --with-deps chromium",
        "npm run test:e2e:ci",
    ):
        require(fragment in validate_workflow, f"React validation workflow lacks: {fragment}")

    deploy_workflow = (ROOT / ".github" / "workflows" / "deploy-prod.yml").read_text(encoding="utf-8")
    deploy_all = (ROOT / "scripts" / "deploy-all.ps1").read_text(encoding="utf-8")
    deploy_web = (ROOT / "scripts" / "deploy-web.ps1").read_text(encoding="utf-8")
    require("npm audit --audit-level=high" in deploy_web, "Production web deployment lacks the dependency vulnerability gate.")
    for prohibited in ("skip_ui_tests", "SKIP_UI_TESTS", "SkipUiTests"):
        require(prohibited not in deploy_workflow + deploy_all, f"Production UI test bypass remains: {prohibited}")
    require("SkipTests" not in deploy_web, "Production web deployment must not permit skipping tests.")
    require("--if-present" not in deploy_web, "Production web tests must fail closed when scripts are missing.")
    for fragment in ("npm run typecheck", "npm run test:coverage", "npm run test:e2e:ci"):
        require(fragment in deploy_web, f"Production web deployment lacks: {fragment}")

    if not package_path.is_file():
        return

    package = load_json(package_path)
    require((web / "package-lock.json").is_file(), "React package-lock.json is required.")
    scripts = package.get("scripts", {})
    for name in ("lint", "typecheck", "test:coverage", "build", "test:e2e", "test:e2e:ci"):
        require(isinstance(scripts.get(name), str) and scripts[name], f"React package script is required: {name}")
    dependencies = {**package.get("dependencies", {}), **package.get("devDependencies", {})}
    for name in ("@axe-core/playwright", "@playwright/test", "@vitest/coverage-v8", "msw"):
        require(name in dependencies, f"React test dependency is required: {name}")

    vitest_path = web / "vitest.config.ts"
    playwright_path = web / "playwright.config.ts"
    require(vitest_path.is_file(), "React Vitest coverage configuration is required.")
    require(playwright_path.is_file(), "React Playwright configuration is required.")
    vitest = vitest_path.read_text(encoding="utf-8")
    require(re.search(r"perFile\s*:\s*true", vitest) is not None, "Vitest coverage must be per-file.")
    for metric in ("lines", "statements", "functions", "branches"):
        require(
            re.search(rf"{metric}\s*:\s*(?:8[0-9]|9[0-9]|100)\b", vitest) is not None,
            f"Vitest per-file {metric} coverage must be at least 80%.",
        )
    require(any((web / "e2e").rglob("*.spec.ts")), "At least one Playwright integration test is required.")


def validate_azure_control_plane() -> None:
    """Keep Azure as the sole public API and AWS as a private headless data plane."""

    feature = load_json(ROOT / ".specify" / "feature.json")
    active_feature_path = feature.get("feature_directory")
    require(
        isinstance(active_feature_path, str) and active_feature_path.startswith("specs/"),
        "The active feature must remain under specs/ while Azure control-plane invariants are checked.",
    )

    for relative_path in (
        "infra/azure/main.bicep",
        "infra/azure/acr-build-api.yml",
        "services/azure_api/main.py",
        "services/azure_api/auth.py",
        "services/azure_api/aws_credentials.py",
        "services/azure_api/settings.py",
        "services/azure_api/Dockerfile",
        "services/azure_api/requirements.txt",
        "scripts/deploy-azure.ps1",
        "scripts/deploy-all.ps1",
        "scripts/deploy-web.ps1",
        "scripts/cutover-api-domain.ps1",
        "scripts/provision-entra-federation.ps1",
    ):
        require((ROOT / relative_path).is_file(), f"Azure control-plane artifact is missing: {relative_path}")

    dockerfile = (ROOT / "services" / "azure_api" / "Dockerfile").read_text(encoding="utf-8")
    dockerignore = (ROOT / ".dockerignore").read_text(encoding="utf-8")
    for sensitive_pattern in ("**/.env", "**/*.pem", "**/*.key", "**/*.pfx", "**/*.pdf"):
        require(
            sensitive_pattern in dockerignore,
            f"ACR build context does not exclude sensitive file pattern: {sensitive_pattern}",
        )
    require(
        re.search(r"^FROM\s+python:[^\s]+@sha256:[0-9a-f]{64}$", dockerfile, re.MULTILINE) is not None,
        "Azure API base image must be pinned by immutable digest.",
    )
    require(
        "FROM python:3.13.14-slim-bookworm@sha256:9d7f287598e1a5a978c015ee176d8216435aaf335ed69ac3c38dd1bbb10e8d64"
        in dockerfile,
        "Azure API must use the reviewed security-clean Python base digest.",
    )
    require("USER 10001:10001" in dockerfile, "Azure API container must run as the dedicated non-root user.")
    require("--no-access-log" in dockerfile, "Azure API must not log business identifiers from raw request paths.")
    require(
        "--mount=type=secret,id=enterprise_ca,required=false" in dockerfile,
        "Enterprise CA support must use an ephemeral BuildKit secret.",
    )
    for frontend in re.findall(r"^#\s*syntax\s*=\s*(\S+)\s*$", dockerfile, re.MULTILINE):
        require(
            re.search(r"@sha256:[0-9a-f]{64}$", frontend) is not None,
            "A Dockerfile syntax frontend must be pinned by immutable digest.",
        )
    for prohibited in ("--trusted-host", "PIP_TRUSTED_HOST", "PIP_NO_VERIFY"):
        require(prohibited not in dockerfile, f"Docker TLS verification bypass is prohibited: {prohibited}")

    acr_task_path = ROOT / "infra" / "azure" / "acr-build-api.yml"
    acr_task = yaml.safe_load(acr_task_path.read_text(encoding="utf-8"))
    require(isinstance(acr_task, dict), "The ACR API image task must be a YAML mapping.")
    require(acr_task.get("version") == "v1.1.0", "The ACR API image task must use schema version v1.1.0.")
    task_environment = acr_task.get("env")
    require(
        isinstance(task_environment, list) and "DOCKER_BUILDKIT=1" in task_environment,
        "The ACR API image task must explicitly enable BuildKit.",
    )
    task_steps = acr_task.get("steps")
    require(isinstance(task_steps, list), "The ACR API image task must define ordered steps.")
    expected_acr_image = "$Registry/{{.Values.image}}"
    build_positions = [
        index for index, step in enumerate(task_steps) if isinstance(step, dict) and "build" in step
    ]
    push_positions = [
        index for index, step in enumerate(task_steps) if isinstance(step, dict) and "push" in step
    ]
    require(len(build_positions) == 1, "The ACR API image task must define exactly one build step.")
    require(len(push_positions) == 1, "The ACR API image task must define exactly one push step.")
    require(build_positions[0] < push_positions[0], "The ACR API image task must push only after building.")
    build_command = " ".join(str(task_steps[build_positions[0]]["build"]).split())
    require(
        f"--tag {expected_acr_image}" in build_command
        and "--file services/azure_api/Dockerfile" in build_command
        and build_command.endswith(" ."),
        "The ACR API image task must build the reviewed Dockerfile from the repository root.",
    )
    pushed_images = task_steps[push_positions[0]]["push"]
    require(
        isinstance(pushed_images, list) and pushed_images == [expected_acr_image],
        "The ACR API image task must explicitly push the parameterized registry image.",
    )

    runtime_requirements = (ROOT / "services" / "azure_api" / "requirements.txt").read_text(encoding="utf-8")
    development_requirements = (ROOT / "requirements-dev.txt").read_text(encoding="utf-8")
    for required_pin in (
        "azure-identity==1.25.3",
        "boto3==1.43.52",
        "fastapi==0.139.2",
        "PyJWT[crypto]==2.13.0",
        "starlette==1.3.1",
        "uvicorn==0.51.0",
    ):
        require(required_pin in runtime_requirements, f"Reviewed Azure runtime pin is missing: {required_pin}")
    for required_pin in (
        "azure-identity==1.25.3",
        "boto3==1.43.52",
        "fastapi==0.139.2",
        "httpx2==2.7.0",
        "PyJWT[crypto]==2.13.0",
        "starlette==1.3.1",
    ):
        require(required_pin in development_requirements, f"Development/runtime pin is inconsistent: {required_pin}")
    require(
        "httpx==0.28.1" not in development_requirements.splitlines(),
        "Starlette 1.3.1 TestClient must use its preferred httpx2 compatibility package.",
    )

    for retired_path in ("scripts/deploy-edge.ps1", "infra/edge/template.yaml"):
        require(
            not (ROOT / retired_path).exists(),
            f"Retired AWS edge deployment source must not remain runnable: {retired_path}",
        )

    lock = load_json(ROOT / "vendor" / "idp.lock.json")
    require(lock.get("deploymentMode") == "headless", "The pinned IDP deployment must remain headless.")
    for package, key in (("cfn-lint", "cfnLint"), ("ruff", "ruff")):
        require(
            f"{package}=={lock.get('cliBuildTools', {}).get(key, '')}" in development_requirements,
            f"IDP publisher pin for {package} must match requirements-dev.txt.",
        )
    idp_deploy = (ROOT / "scripts" / "deploy-idp.ps1").read_text(encoding="utf-8")
    require("--headless" in idp_deploy, "The IDP deployment script must enforce --headless.")
    validate_idp_python_toolchain_contract(
        lock,
        idp_deploy,
        (ROOT / "scripts" / "bootstrap.ps1").read_text(encoding="utf-8"),
        (ROOT / ".github" / "workflows" / "deploy-prod.yml").read_text(encoding="utf-8"),
        (ROOT / ".github" / "workflows" / "validate.yml").read_text(encoding="utf-8"),
    )
    validate_idp_windows_bridge_contract(
        idp_deploy,
        (ROOT / "scripts" / "common.psm1").read_text(encoding="utf-8"),
        (ROOT / "scripts" / "idp_windows_cli_bridge" / "pyproject.toml").read_text(
            encoding="utf-8"
        ),
        (ROOT / "scripts" / "idp_windows_cli_bridge" / "idp_windows_cli_bridge.py").read_text(
            encoding="utf-8"
        ),
    )

    aws_template = (ROOT / "infra" / "api" / "template.yaml").read_text(encoding="utf-8")
    for prohibited in (
        "AWS::ApiGateway",
        "AWS::AppSync",
        "AWS::CloudFront",
        "LoanApiFunction:",
        "appsync:",
    ):
        require(prohibited not in aws_template, f"Obsolete AWS public API surface remains: {prohibited}")
    for required_fragment in (
        "EntraTenantOidcProvider:",
        "AzureApiRuntimeRole:",
        "RolePermissionsBoundaryArn:",
        "PermissionsBoundary: !Ref RolePermissionsBoundaryArn",
        "sts:AssumeRoleWithWebIdentity",
        "/:aud",
        "/:sub",
        "${SourceBucket.Arn}/quarantine/tenants/*",
        "prefix: quarantine/tenants/",
        "UploadCompletionStreamMapping:",
        "Type: AWS::Lambda::EventSourceMapping",
        "EventSourceArn: !GetAtt RegistryTable.StreamArn",
        "dynamodb:GetRecords",
        "BisectBatchOnFunctionError: true",
        "StartingPosition: TRIM_HORIZON",
        "Destination: !GetAtt UploadProcessorDlq.Arn",
        "ReportBatchItemFailures",
    ):
        require(required_fragment in aws_template, f"AWS federation template lacks: {required_fragment}")
    require(
        aws_template.count("PermissionsBoundary: !Ref RolePermissionsBoundaryArn") == 5,
        "Every platform-created IAM role must use the bootstrap permissions boundary.",
    )
    validate_platform_event_source_filters(aws_template)

    bootstrap_template = (ROOT / "infra" / "bootstrap" / "template.yaml").read_text(encoding="utf-8")
    for required_fragment in (
        "PlatformCloudFormationExecutionRole:",
        "IdpCloudFormationExecutionRole:",
        "PlatformRolePermissionsBoundary:",
        "IdpRolePermissionsBoundary:",
        "iam:PermissionsBoundary:",
        "iam:PolicyARN:",
        "iam:PassedToService:",
        "DenyPlatformBoundaryRemoval",
        "DenyIdpBoundaryRemoval",
        "stack/${PlatformStackName}/*",
        "stack/${IdpStackName}/*",
        "arn:${AWS::Partition}:cloudformation:${AWS::Region}:aws:transform/Serverless-2016-10-31",
    ):
        require(required_fragment in bootstrap_template, f"AWS deployment least privilege lacks: {required_fragment}")
    validate_serverless_transform_execution_roles(bootstrap_template)
    validate_platform_cloudformation_handler_contract(aws_template, bootstrap_template)
    require(
        "\n  CloudFormationExecutionRole:\n" not in bootstrap_template,
        "The obsolete shared CloudFormation execution role must not be restored.",
    )

    stack_policy = load_json(ROOT / "infra" / "stack-policies" / "protect-stateful-resources.json")
    protected_types: set[str] = set()
    protected_actions: set[str] = set()
    for statement in stack_policy.get("Statement", []):
        if statement.get("Effect") != "Deny":
            continue
        actions = statement.get("Action", [])
        protected_actions.update([actions] if isinstance(actions, str) else actions)
        protected_types.update(statement.get("Condition", {}).get("StringEquals", {}).get("ResourceType", []))
    require(
        {"Update:Delete", "Update:Replace"}.issubset(protected_actions),
        "Stateful stack policy must deny replacement and deletion.",
    )
    require(
        {"AWS::DynamoDB::Table", "AWS::KMS::Key", "AWS::S3::Bucket"}.issubset(protected_types),
        "Stateful stack policy must protect DynamoDB, KMS, and S3 resource types.",
    )

    deploy_platform = (ROOT / "scripts" / "deploy-platform.ps1").read_text(encoding="utf-8")
    deploy_idp = (ROOT / "scripts" / "deploy-idp.ps1").read_text(encoding="utf-8")
    validate_platform_packaging_contract(deploy_platform)
    for required_fragment in (
        "PlatformCloudFormationExecutionRoleArn",
        "PlatformRolePermissionsBoundaryArn",
        "Set-AwsStatefulStackPolicy",
    ):
        require(required_fragment in deploy_platform, f"Platform deployment gate lacks: {required_fragment}")
    for required_fragment in (
        "IdpCloudFormationExecutionRoleArn",
        "IdpRolePermissionsBoundaryArn",
        "PermissionsBoundaryArn=",
        "Set-AwsStatefulStackPolicy",
    ):
        require(required_fragment in deploy_idp, f"IDP deployment gate lacks: {required_fragment}")

    loan_runtime = (ROOT / "services" / "loan_api" / "app.py").read_text(encoding="utf-8")
    require(
        'key = f"quarantine/tenants/' in loan_runtime,
        "New source uploads must use the GuardDuty-protected top-level quarantine prefix.",
    )
    for prohibited in ("boto3.resource(", "boto3.client("):
        require(prohibited not in loan_runtime, f"Loan domain constructs an ambient AWS dependency: {prohibited}")
    for required_fragment in (
        "connect_timeout=3",
        "read_timeout=10",
        'retries={"mode": "standard", "total_max_attempts": 3}',
        "tcp_keepalive=True",
        "MAXIMUM_QUERY_ITEMS",
        "MAXIMUM_LOAN_ARCHIVE_DOCUMENTS",
        "MAXIMUM_LOAN_ARCHIVE_MANIFEST_BYTES",
    ):
        require(required_fragment in loan_runtime, f"Loan runtime hardening lacks: {required_fragment}")

    azure_bicep = (ROOT / "infra" / "azure" / "main.bicep").read_text(encoding="utf-8")
    for required_fragment in (
        "Microsoft.App/containerApps",
        "Microsoft.ManagedIdentity/userAssignedIdentities",
        "Microsoft.ContainerRegistry/registries",
        "Microsoft.Web/staticSites",
        "apiCustomDomainCertificateId",
        "customDomains:",
        "param maximumQueryItems int = 5000",
        "param maximumLoanArchiveDocuments int = 500",
        "param maximumLoanArchiveManifestBytes int = 4194304",
        "name: 'MAXIMUM_QUERY_ITEMS'",
        "name: 'MAXIMUM_LOAN_ARCHIVE_DOCUMENTS'",
        "name: 'MAXIMUM_LOAN_ARCHIVE_MANIFEST_BYTES'",
    ):
        require(required_fragment in azure_bicep, f"Azure Bicep lacks: {required_fragment}")

    azure_runtime = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (ROOT / "services" / "azure_api").glob("*.py")
    )
    require("HOST_NOT_ALLOWED" in azure_runtime, "Production product routes must enforce the custom API hostname.")
    for prohibited in (
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "LOAN_API_ORIGIN_VERIFY_SECRET",
        "AppSync",
    ):
        require(prohibited not in azure_runtime, f"Azure runtime contains a prohibited integration: {prohibited}")

    active_deployment_files = [
        *sorted((ROOT / "scripts").glob("*.ps1")),
        *sorted((ROOT / ".github" / "workflows").glob("*.yml")),
        ROOT / "infra" / "bootstrap" / "template.yaml",
    ]
    active_deployment = "\n".join(
        path.read_text(encoding="utf-8") for path in active_deployment_files if path.is_file()
    )
    for prohibited in (
        "LOAN_API_ORIGIN_VERIFY_SECRET",
        "OriginVerifySecret",
        "deploy-edge.ps1",
        "SkipEdge",
        "cloudfront:*",
        "apigateway:*",
        "wafv2:*",
        "wafv2.amazonaws.com",
    ):
        require(
            prohibited not in active_deployment,
            f"Retired AWS public-edge integration remains runnable: {prohibited}",
        )

    deploy_all = (ROOT / "scripts" / "deploy-all.ps1").read_text(encoding="utf-8")
    for required_fragment in ("deploy-azure.ps1", "deploy-platform.ps1"):
        require(required_fragment in deploy_all, f"Deployment orchestrator lacks: {required_fragment}")
    deploy_web = (ROOT / "scripts" / "deploy-web.ps1").read_text(encoding="utf-8")
    require("staticwebapp" in deploy_web.lower(), "Web deployment must target Azure Static Web Apps.")
    for prohibited in ("cloudfront", "s3 sync", "UiDistributionId"):
        require(prohibited.lower() not in deploy_web.lower(), f"Web deployment still targets AWS edge hosting: {prohibited}")

    deploy_azure = (ROOT / "scripts" / "deploy-azure.ps1").read_text(encoding="utf-8")
    for required_fragment in (
        "az acr run",
        "infra/azure/acr-build-api.yml",
        '--set "image=${ImageRepository}:$ImageTag"',
        "trivy image",
        "--severity HIGH,CRITICAL",
        "--ignore-unfixed",
        "--format cyclonedx",
        "Production deployment cannot skip",
        "Get-LiveApiCustomDomainBinding",
        "dnsCutoverPerformed",
        "maximumQueryItems",
        "maximumLoanArchiveDocuments",
        "maximumLoanArchiveManifestBytes",
    ):
        require(required_fragment in deploy_azure, f"Exact-image production gate lacks: {required_fragment}")
    require("az acr build" not in deploy_azure, "Production image builds must use the explicit BuildKit ACR task.")
    cutover = (ROOT / "scripts" / "cutover-api-domain.ps1").read_text(encoding="utf-8")
    require("azure.api.imageScan" in cutover, "API DNS cutover must verify exact-image scan evidence.")
    trivy_installer = (ROOT / "scripts" / "install-trivy.ps1").read_text(encoding="utf-8")
    for required_fragment in (
        "$version = '0.72.0'",
        "$expectedSha256 = 'bbb64b9695866ce4a7a8f5c9592002c5961cab378577fa3f8a040df362b9b2ea'",
        "github.com/aquasecurity/trivy/releases/download/v$version/$assetName",
        "Get-FileHash -LiteralPath $archivePath -Algorithm SHA256",
    ):
        require(required_fragment in trivy_installer, f"Trivy installer pin lacks: {required_fragment}")
    production_workflow = (ROOT / ".github" / "workflows" / "deploy-prod.yml").read_text(encoding="utf-8")
    for required_fragment in (
        "run: ./scripts/install-trivy.ps1",
    ):
        require(required_fragment in production_workflow, f"Production scanner pin lacks: {required_fragment}")
    require(
        "aquasecurity/setup-trivy@" not in production_workflow,
        "Production workflow uses an action outside the repository's selected-action allowlist.",
    )
    validation_workflow = (ROOT / ".github" / "workflows" / "validate.yml").read_text(encoding="utf-8")
    for required_fragment in (
        "run: ./scripts/install-trivy.ps1",
        "trivy image --scanners vuln --severity HIGH,CRITICAL --ignore-unfixed --exit-code 1",
        "trivy image --format cyclonedx",
    ):
        require(required_fragment in validation_workflow, f"Pull-request image gate lacks: {required_fragment}")
    require(
        "aquasecurity/setup-trivy@" not in validation_workflow,
        "Validation workflow uses an action outside the repository's selected-action allowlist.",
    )
    validation_document = yaml.load(validation_workflow, Loader=yaml.BaseLoader)
    validation_jobs = validation_document.get("jobs", {}) if isinstance(validation_document, dict) else {}
    require(isinstance(validation_jobs, dict), "Validation workflow jobs must be a mapping.")
    docker_build_steps = [
        step
        for job in validation_jobs.values()
        if isinstance(job, dict)
        for step in job.get("steps", [])
        if isinstance(step, dict) and "docker build" in str(step.get("run", ""))
    ]
    require(len(docker_build_steps) == 1, "Validation must contain exactly one Docker image build step.")
    build_environment = docker_build_steps[0].get("env", {})
    require(
        isinstance(build_environment, dict) and build_environment.get("DOCKER_BUILDKIT") == "1",
        "Validation Docker builds must explicitly enable BuildKit.",
    )


def validate_markdown_links(path: Path) -> None:
    content = path.read_text(encoding="utf-8")
    for raw_target in re.findall(r"(?<!!)\[[^\]]+\]\(([^)]+)\)", content):
        target = raw_target.strip().strip("<>")
        if not target or target.startswith(("#", "http://", "https://", "mailto:")):
            continue
        relative_target = unquote(target.split("#", 1)[0])
        resolved_target = resolve_repository_path(path.parent, relative_target, "Markdown link")
        require(
            resolved_target.exists(),
            f"Broken local Markdown link in {path}: {target}",
        )


def validate_spec_kit() -> None:
    lock = load_json(ROOT / "vendor" / "spec-kit.lock.json")
    require(lock["repository"] == "https://github.com/github/spec-kit", "Unexpected Spec Kit source.")
    require(lock["version"] == SPEC_KIT_VERSION, "Spec Kit version changed without review.")
    require(lock["tag"] == f"v{SPEC_KIT_VERSION}", "Spec Kit tag does not match its version.")
    require(lock["commit"] == SPEC_KIT_COMMIT, "Spec Kit commit changed without review.")
    require(lock["integration"] == "claude", "Spec Kit must use the Claude integration.")
    require(lock["script"] == "ps", "Spec Kit must use PowerShell scripts on this repository.")
    require(lock["aiSkills"] is True, "Spec Kit Claude skills must remain enabled.")

    init_options = load_json(ROOT / ".specify" / "init-options.json")
    require(init_options["speckit_version"] == SPEC_KIT_VERSION, "Generated Spec Kit version drifted.")
    require(init_options["integration"] == "claude", "Generated integration must remain Claude.")
    require(init_options["ai_skills"] is True, "Claude skills must remain enabled.")
    require(init_options["script"] == "ps", "Generated scripts must remain PowerShell.")

    active_feature = load_json(ROOT / ".specify" / "feature.json")
    active_feature_path = active_feature.get("feature_directory")
    require(isinstance(active_feature_path, str) and active_feature_path, "Active feature path is missing.")
    active_feature_dir = resolve_repository_path(ROOT, active_feature_path, "active feature path")
    require(
        active_feature_dir.is_relative_to((ROOT / "specs").resolve()),
        "The active feature must remain under specs/.",
    )
    require(active_feature_dir.is_dir(), f"Active feature directory is missing: {active_feature_path}")
    for required_name in ("spec.md", "plan.md", "tasks.md"):
        require(
            (active_feature_dir / required_name).is_file(),
            f"Active feature is missing {required_name}: {active_feature_path}",
        )

    shared_assets = [
        ".specify/integration.json",
        ".specify/integrations/claude.manifest.json",
        ".specify/integrations/speckit.manifest.json",
        ".specify/memory/constitution.md",
        ".specify/scripts/powershell/check-prerequisites.ps1",
        ".specify/scripts/powershell/common.ps1",
        ".specify/scripts/powershell/create-new-feature.ps1",
        ".specify/scripts/powershell/setup-plan.ps1",
        ".specify/scripts/powershell/setup-tasks.ps1",
        ".specify/templates/checklist-template.md",
        ".specify/templates/constitution-template.md",
        ".specify/templates/plan-template.md",
        ".specify/templates/spec-template.md",
        ".specify/templates/tasks-template.md",
        ".specify/workflows/speckit/workflow.yml",
        ".specify/workflows/workflow-registry.json",
    ]
    for relative_path in shared_assets:
        require((ROOT / relative_path).is_file(), f"Missing Spec Kit shared asset: {relative_path}")

    for manifest_name, expected_integration in (
        ("claude.manifest.json", "claude"),
        ("speckit.manifest.json", "speckit"),
    ):
        manifest_path = ROOT / ".specify" / "integrations" / manifest_name
        manifest = load_json(manifest_path)
        require(manifest["integration"] == expected_integration, f"Integration mismatch in {manifest_path}")
        require(manifest["version"] == SPEC_KIT_VERSION, f"Version mismatch in {manifest_path}")
        require(isinstance(manifest.get("files"), dict), f"Missing generated file map in {manifest_path}")
        for relative_path, expected_sha256 in manifest["files"].items():
            generated_path = ROOT / relative_path
            require(generated_path.is_file(), f"Missing generated Spec Kit file: {relative_path}")
            require(
                normalized_text_sha256(generated_path) == expected_sha256,
                f"Generated Spec Kit file differs from its manifest: {relative_path}",
            )

    for skill_name in SPEC_KIT_SKILLS:
        path = ROOT / ".claude" / "skills" / skill_name / "SKILL.md"
        require(path.is_file(), f"Missing Claude Code skill: {path}")
        content = path.read_text(encoding="utf-8")
        require(content.startswith("---\n") or content.startswith("---\r\n"), f"Missing skill frontmatter: {path}")
        parts = re.split(r"^---\s*$", content, maxsplit=2, flags=re.MULTILINE)
        require(len(parts) >= 3, f"Invalid skill frontmatter: {path}")
        metadata = yaml.safe_load(parts[1])
        require(isinstance(metadata, dict), f"Invalid skill metadata: {path}")
        require(metadata.get("name") == skill_name, f"Claude skill name mismatch: {path}")
        require(metadata.get("user-invocable") is True, f"Claude skill must be user-invocable: {path}")
        require(
            metadata.get("disable-model-invocation") is False,
            f"Claude skill must allow model invocation: {path}",
        )
        for unresolved_token in ("{SCRIPT}", "{ARGS}", "__AGENT__", "__SPECKIT_COMMAND_"):
            require(unresolved_token not in content, f"Unrendered integration token in {path}")

    authored_artifacts = [
        "specs/README.md",
        "specs/001-loan-document-platform/spec.md",
        "specs/001-loan-document-platform/plan.md",
        "specs/001-loan-document-platform/research.md",
        "specs/001-loan-document-platform/data-model.md",
        "specs/001-loan-document-platform/quickstart.md",
        "specs/001-loan-document-platform/tasks.md",
        "specs/001-loan-document-platform/contracts/README.md",
        "specs/001-loan-document-platform/checklists/requirements.md",
        "specs/001-loan-document-platform/checklists/security.md",
        "specs/001-loan-document-platform/checklists/production-readiness.md",
        "specs/002-azure-api-control-plane/spec.md",
        "specs/002-azure-api-control-plane/plan.md",
        "specs/002-azure-api-control-plane/research.md",
        "specs/002-azure-api-control-plane/data-model.md",
        "specs/002-azure-api-control-plane/quickstart.md",
        "specs/002-azure-api-control-plane/tasks.md",
        "specs/002-azure-api-control-plane/contracts/README.md",
        "specs/002-azure-api-control-plane/checklists/requirements.md",
        ".claude/README.md",
        ".specify/README.md",
        ".github/copilot-instructions.md",
        "docs/spec-driven-development.md",
    ]
    unresolved_tokens = (
        "[FEATURE NAME]",
        "[###-feature-name]",
        "[YYYY-MM-DD]",
        "[NEEDS CLARIFICATION",
        "[Link to research.md]",
    )
    for relative_path in authored_artifacts:
        path = ROOT / relative_path
        require(path.is_file(), f"Missing project-owned specification artifact: {relative_path}")
        content = path.read_text(encoding="utf-8")
        for unresolved_token in unresolved_tokens:
            require(unresolved_token not in content, f"Unresolved template token in {path}: {unresolved_token}")
        validate_markdown_links(path)

    constitution = (ROOT / ".specify" / "memory" / "constitution.md").read_text(encoding="utf-8")
    version_match = re.search(r"\*\*Version\*\*: (\d+)\.(\d+)\.(\d+)", constitution)
    require(version_match is not None, "Project constitution must declare a semantic version.")
    constitution_version = tuple(int(part) for part in version_match.groups())
    require(constitution_version >= (1, 2, 0), "Project constitution predates mandatory coverage gates.")
    require("Mandatory Exact-Head Copilot Review" in constitution, "Constitution lacks Copilot governance.")
    require("Mandatory Coverage and Browser Integration" in constitution, "Constitution lacks coverage governance.")


def validate_environment_configuration_script(
    configurator: str,
    common: str,
    bootstrap: str,
    gitignore: str,
    constitution: str,
) -> None:
    required_fragments = (
        "#requires -Version 7.2",
        "Invoke-AzureCli -Arguments",
        "Assert-AzureIdentity -Account $azureAccount",
        "Invoke-Aws -Profile",
        "Assert-CertificateOnlyBundle -Path $bundleCandidate",
        "Assert-ExistingValueMatches -Config $config -Name 'azureSubscriptionId'",
        "Assert-ExistingValueMatches -Config $config -Name 'entraTenantId'",
        "Assert-ExistingValueMatches -Config $config -Name 'awsAccountId'",
        "'check-ignore', '--quiet'",
        "Where-Object { -not [bool]$_.Config.PrivateZone }",
        "Read-EnvironmentConfig -Path $temporaryPath",
        "[System.Text.UTF8Encoding]::new($false)",
        "[System.IO.File]::Replace($validatedConfigPath, $resolvedEnvironmentFile, $backupConfigPath, $true)",
        "$env:REQUESTS_CA_BUNDLE = $resolvedBundle",
        "$env:SSL_CERT_FILE = $resolvedBundle",
        "$env:AWS_CA_BUNDLE = $resolvedBundle",
        "Remove-Item -LiteralPath $validatedConfigPath -Force -WhatIf:$false",
        "Read-Host 'Route 53 hosted-zone ID' -MaskInput",
        "Read-Host 'UI hostname (leave blank for the deterministic default)' -MaskInput",
        "Read-Host 'API hostname (leave blank for the deterministic default)' -MaskInput",
        "Cloud identifiers, contacts, profile names, and the complete configuration were not displayed.",
        "([string]$AwsProfile).Trim()",
        "([string]$HostedZoneId).Trim()",
        "([string]$UiHostName).Trim()",
        "([string]$ApiHostName).Trim()",
        "([string]$AzureContainerRegistryName).Trim()",
    )
    for fragment in required_fragments:
        require(
            fragment in configurator,
            f"Repository-owned environment configuration lacks: {fragment}",
        )
    for forbidden_fragment in (
        "Assert-AwsIdentity",
        "C:\\Users\\",
        "Set-Content -LiteralPath $resolvedEnvironmentFile",
        'Write-Host "AWS identity:',
        "$publicZones[$index].Name",
        'Read-Host "UI hostname [$defaultUiHost]"',
        'Read-Host "API hostname [$defaultApiHost]"',
        "$selectedAlertEmail = $AlertEmail.Trim()",
        "$selectedBudgetEmail = $BudgetEmail.Trim()",
        "$AwsProfile.Trim()",
        "$HostedZoneId.Trim()",
        "$UiHostName.Trim()",
        "$ApiHostName.Trim()",
        "$AzureContainerRegistryName.Trim()",
    ):
        require(
            forbidden_fragment not in configurator,
            f"Repository-owned environment configuration exposes or weakens: {forbidden_fragment}",
        )
    require(
        configurator.count(") -CaptureJson -ForceProfile") == 2,
        "Repository-owned environment configuration must force the selected AWS profile for identity and Route 53 reads.",
    )
    for fragment in (
        "function Assert-CertificateOnlyBundle",
        "$pemBoundaries | Where-Object { $_.Groups['Label'].Value -cne 'CERTIFICATE' }",
        "X509Certificate2]::new(",
        "function Assert-AzureIdentity",
        "Azure account lookup returned invalid identity identifiers.",
        "[switch]$ForceProfile",
        "if ($ForceProfile -or $env:GITHUB_ACTIONS -ne 'true')",
        "$output = @(& $launch.FilePath @allArguments 2>$null)",
        "PSNativeCommandUseErrorActionPreference",
        "Write-Host 'AWS identity and region verified.'",
    ):
        require(fragment in common, f"Shared configuration safety lacks: {fragment}")
    for forbidden_fragment in ("$identity.Arn", "expected $ExpectedAccountId"):
        require(
            forbidden_fragment not in common,
            f"Shared identity verification exposes cloud identifiers: {forbidden_fragment}",
        )
    for fragment in (
        "#requires -Version 7.2",
        "Assert-CertificateOnlyBundle -Path",
        "Invoke-AzureCli -Arguments",
        "Assert-AzureIdentity @azureIdentityArguments",
        "Resolve-PythonLaunch -Version '3.13'",
    ):
        require(fragment in bootstrap, f"Bootstrap identity/trust safety lacks: {fragment}")
    require(
        "[guid]$azAccount" not in bootstrap,
        "Bootstrap must not cast raw Azure identity values before redacted validation.",
    )
    require(
        "& python --version" not in bootstrap,
        "Bootstrap must verify the resolved platform Python 3.13 runtime, not PATH's generic Python.",
    )
    for variable in ("REQUESTS_CA_BUNDLE", "SSL_CERT_FILE", "AWS_CA_BUNDLE"):
        require(
            f"$env:{variable} = $caPath" in bootstrap,
            f"Bootstrap does not restore the configured {variable} trust path.",
        )
    require(
        "config/environments/*.json" in gitignore
        and "!config/environments/*.example.json" in gitignore,
        "Real environment files must remain ignored while examples stay reviewed.",
    )
    require(
        "successful repeatable cloud-operation procedure" in constitution,
        "Constitution does not require successful operator procedures to become reviewed scripts.",
    )


def main() -> None:
    ignored_roots = {
        ".git",
        ".agents",
        ".codex",
        ".local",
        ".venv",
        "venv",
        "__pycache__",
        "work",
        "outputs",
        "node_modules",
    }
    files = repository_files(ignored_roots)
    for path in (item for item in files if item.suffix.lower() == ".json"):
        load_json(path)

    for path in (ROOT / ".github" / "workflows").glob("*.yml"):
        with path.open("r", encoding="utf-8") as handle:
            workflow = yaml.load(handle, Loader=yaml.BaseLoader)
        require(isinstance(workflow, dict), f"Invalid GitHub workflow: {path}")
        trigger_names = workflow_trigger_names(workflow.get("on"), path)
        require("pull_request_target" not in trigger_names, f"Unsafe pull_request_target trigger in {path}")
        validate_workflow_actions(workflow, path)

    validate_copilot_review_gate()
    validate_python_quality_gate()
    validate_web_quality_gate()
    validate_azure_control_plane()
    validate_environment_configuration_script(
        (ROOT / "scripts" / "configure-environment.ps1").read_text(encoding="utf-8"),
        (ROOT / "scripts" / "common.psm1").read_text(encoding="utf-8"),
        (ROOT / "scripts" / "bootstrap.ps1").read_text(encoding="utf-8"),
        (ROOT / ".gitignore").read_text(encoding="utf-8"),
        (ROOT / ".specify" / "memory" / "constitution.md").read_text(encoding="utf-8"),
    )

    environment_example = load_json(ROOT / "config" / "environments" / "prod.example.json")
    require(
        environment_example["repositoryName"] == GITHUB_REPOSITORY.split("/", 1)[1],
        "Production example must use the canonical GitHub repository name.",
    )
    require(environment_example.get("azureMonthlyBudgetUsd", 0) >= 1, "Azure budget must be explicit.")
    require(environment_example.get("azureContainerAppsZoneRedundant") is True, "Production Container Apps must be zone redundant.")
    require(environment_example.get("azureApiMinReplicas", 0) >= 2, "Production API must keep at least two replicas.")
    require(100 <= environment_example.get("maximumQueryItems", 0) <= 100_000, "Production query limit is invalid.")
    require(
        1 <= environment_example.get("maximumLoanArchiveDocuments", 0) <= environment_example["maximumQueryItems"],
        "Production archive document limit is invalid.",
    )
    require(
        1024 <= environment_example.get("maximumLoanArchiveManifestBytes", 0) <= 20 * 1024 * 1024,
        "Production archive manifest limit is invalid.",
    )
    for relative_path in ("README.md", "docs/github-delivery.md"):
        content = (ROOT / relative_path).read_text(encoding="utf-8")
        require("hdduong/loan-document-platform" not in content, f"Old GitHub slug remains in {relative_path}")
        require(GITHUB_REPOSITORY in content, f"Canonical GitHub repository missing from {relative_path}")

    protection_script = (ROOT / "scripts" / "configure-github-protection.ps1").read_text(
        encoding="utf-8"
    )
    for required_fragment in (
        "Mandatory Copilot review",
        "review_draft_pull_requests = $true",
        "review_on_push = $true",
        "contexts = @('validate', 'copilot-review')",
        "azure/login@532459ea530d8321f2fb9bb10d1e0bcf23869a43",
        "docker/setup-qemu-action@c7c53464625b32c7a7e944ae62b3e17d2b600130",
        "docker/setup-buildx-action@8d2750c68a42422c14e847fe6c8ac0403b4cbd6f",
        "docker/build-push-action@10e90e3645eae34f1e60eeb005ba3a3d33f178e8",
    ):
        require(required_fragment in protection_script, f"GitHub protection lacks: {required_fragment}")
    require(
        "aquasecurity/setup-trivy@" not in protection_script,
        "GitHub protection still allows the retired setup-trivy action.",
    )

    runtime_schema = load_json(ROOT / "contracts" / "runtime-config.schema.json")
    runtime_example = load_json(ROOT / "apps" / "web" / "public" / "runtime-config.example.json")
    Draft202012Validator(runtime_schema, format_checker=FormatChecker()).validate(runtime_example)

    manifest = load_json(IDP_DIR / "manifest.json")
    for name in ("screen", "full"):
        entry = manifest[name]
        path = IDP_DIR / entry["file"]
        require(path.is_file(), f"Missing {name} configuration: {path}")
        require(
            normalized_text_sha256(path) == entry["sourceSha256"],
            f"{path.name} differs from its reviewed manifest digest; regenerate/review the manifest intentionally.",
        )

    screen = load_json(IDP_DIR / manifest["screen"]["file"])
    require(screen["ocr"]["backend"] == "textract", "Screen OCR backend must be Textract.")
    require(screen["ocr"]["features"] == [], "Screen OCR features must remain empty (DetectDocumentText).")
    classification = screen["classification"]
    require(
        classification["maxPagesForClassification"] == "ALL",
        "Screen classification must inspect every package page.",
    )
    require(
        classification["classificationMethod"] == "multimodalPageLevelClassification",
        "Screen classification method changed.",
    )
    require(classification["sectionSplitting"] == "llm_determined", "Screen section splitting changed.")
    require(classification["contextPagesCount"] == "1", "Screen context page count changed.")
    require(
        classification["model"] == "us.amazon.nova-2-lite-v1:0",
        "Screen classification must use the reviewed Nova Lite profile.",
    )
    require(
        screen["extraction"]["model"] == "us.amazon.nova-2-lite-v1:0",
        "Screen evidence extraction must use the reviewed Nova Lite profile.",
    )
    require(screen["assessment"]["enabled"] is False, "Screen assessment must remain disabled.")
    require(screen["evaluation"]["enabled"] is False, "Screen evaluation must remain disabled.")

    closing_disclosure = next(
        item for item in screen["classes"] if item["$id"] == "L053_Closing_Disclosure"
    )
    require(
        len(closing_disclosure["properties"]) == 13,
        "Screen Closing Disclosure schema must remain the reviewed 13-field evidence schema.",
    )

    lock = load_json(ROOT / "vendor" / "idp.lock.json")
    require(lock["version"] == "0.5.16", "IDP version changed without an explicit upgrade review.")
    require(
        lock["commit"] == "1463fb6ff91c9e0169a148b33e6bc85d12bab995",
        "IDP commit changed without an explicit upgrade review.",
    )

    validate_spec_kit()

    prohibited_suffixes = {".pdf", ".tif", ".tiff", ".pfx", ".p12", ".pem", ".key"}
    secret_patterns = {
        "AWS access key": re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
        "GitHub token": re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
        "private key": PRIVATE_KEY_PEM_PATTERN,
    }
    for path in files:
        require(path.suffix.lower() not in prohibited_suffixes, f"Prohibited sensitive/binary file: {path}")
        if path.stat().st_size > 5 * 1024 * 1024:
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for label, pattern in secret_patterns.items():
            require(pattern.search(content) is None, f"Possible {label} in public source file: {path}")

    print("Repository configuration invariants passed.")


if __name__ == "__main__":
    main()
