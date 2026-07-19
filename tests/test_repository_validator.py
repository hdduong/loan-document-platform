from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

TRANSFORM_ARN = (
    "arn:${AWS::Partition}:cloudformation:${AWS::Region}:aws:"
    "transform/Serverless-2016-10-31"
)


def platform_api_handler_template(
    table_name: str = "loan-document-${EnvironmentName}-registry",
    bucket_name: str = (
        "loan-document-${EnvironmentName}-source-${AWS::AccountId}-${AWS::Region}"
    ),
) -> str:
    return f"""Parameters:
  EnvironmentName:
    Type: String
    AllowedPattern: '[a-z0-9-]+'
    MaxLength: 13
Resources:
  DataKey:
    Type: AWS::KMS::Key
    Properties:
      Tags:
        - Key: KeyPurpose
          Value: document-data
  SourceBucket:
    Type: AWS::S3::Bucket
    Properties:
      BucketName: !Sub '{bucket_name}'
  RegistryTable:
    Type: AWS::DynamoDB::Table
    Properties:
      TableName: !Sub '{table_name}'
"""


def bootstrap_transform_template(
    *,
    platform_effect: str = "Allow",
    platform_action: str = "cloudformation:CreateChangeSet",
    platform_resource: str = TRANSFORM_ARN,
    include_idp_statement: bool = True,
    duplicate_platform_statement: bool = False,
    deny_platform_transform: bool = False,
) -> str:
    platform_duplicate = (
        f"""
              - Sid: ApplyAwsServerlessTransformAgain
                Effect: Allow
                Action: cloudformation:CreateChangeSet
                Resource: !Sub '{TRANSFORM_ARN}'"""
        if duplicate_platform_statement
        else ""
    )
    idp_statement = (
        f"""
              - Sid: ApplyAwsServerlessTransform
                Effect: Allow
                Action: cloudformation:CreateChangeSet
                Resource: !Sub '{TRANSFORM_ARN}'"""
        if include_idp_statement
        else ""
    )
    platform_deny = (
        """
              - Sid: BlockAwsServerlessTransform
                Effect: Deny
                Action: cloudformation:CreateChangeSet
                Resource: '*'"""
        if deny_platform_transform
        else ""
    )
    return f"""Parameters:
  EnvironmentName:
    Type: String
    AllowedPattern: '[a-z0-9-]+'
    MaxLength: 13
Resources:
  ArtifactKey:
    Type: AWS::KMS::Key
    Properties:
      Tags:
        - Key: KeyPurpose
          Value: deployment-artifacts
  ArtifactBucket:
    Type: AWS::S3::Bucket
    Properties:
      BucketName: !Sub 'loan-document-${{EnvironmentName}}-ci-artifacts-${{AWS::AccountId}}-${{AWS::Region}}'
      BucketEncryption:
        ServerSideEncryptionConfiguration:
          - BucketKeyEnabled: true
            ServerSideEncryptionByDefault:
              SSEAlgorithm: aws:kms
              KMSMasterKeyID: !GetAtt ArtifactKey.Arn
  PlatformCloudFormationExecutionRole:
    Type: AWS::IAM::Role
    Metadata:
      cfn-lint:
        config:
          ignore_checks:
            - W3037
    Properties:
      Policies:
        - PolicyName: PlatformCloudFormation
          PolicyDocument:
            Statement:
              - Sid: ExactPlatformResources
                Effect: Allow
                Action:
                  - dynamodb:*
                  - s3:*
                Resource:
                  - !Sub 'arn:${{AWS::Partition}}:dynamodb:${{AWS::Region}}:${{AWS::AccountId}}:table/loan-document-${{EnvironmentName}}-registry'
                  - !Sub 'arn:${{AWS::Partition}}:dynamodb:${{AWS::Region}}:${{AWS::AccountId}}:table/loan-document-${{EnvironmentName}}-registry/*'
                  - !Sub 'arn:${{AWS::Partition}}:s3:::loan-document-${{EnvironmentName}}-source-${{AWS::AccountId}}-${{AWS::Region}}'
                  - !Sub 'arn:${{AWS::Partition}}:s3:::loan-document-${{EnvironmentName}}-source-${{AWS::AccountId}}-${{AWS::Region}}/*'
              - Sid: ReadPlatformDeploymentArtifacts
                Effect: Allow
                Action: s3:GetObject
                Resource: !Sub '${{ArtifactBucket.Arn}}/platform/${{EnvironmentName}}/*'
              - Sid: DecryptPlatformDeploymentArtifacts
                Effect: Allow
                Action: kms:Decrypt
                Resource: !GetAtt ArtifactKey.Arn
                Condition:
                  StringEquals:
                    kms:ViaService: !Sub 's3.${{AWS::Region}}.${{AWS::URLSuffix}}'
                    kms:EncryptionContext:aws:s3:arn: !GetAtt ArtifactBucket.Arn
              - Sid: CreateTaggedDataKey
                Effect: Allow
                Action: kms:CreateKey
                Resource: '*'
                Condition:
                  StringEquals:
                    aws:RequestTag/Application: loan-document-platform
                    aws:RequestTag/Environment: !Ref EnvironmentName
                    aws:RequestTag/KeyPurpose: document-data
              - Sid: ManageTaggedDataKeys
                Effect: Allow
                Action: kms:*
                Resource: !Sub 'arn:${{AWS::Partition}}:kms:${{AWS::Region}}:${{AWS::AccountId}}:key/*'
                Condition:
                  StringEquals:
                    aws:ResourceTag/Application: loan-document-platform
                    aws:ResourceTag/Environment: !Ref EnvironmentName
                    aws:ResourceTag/KeyPurpose: document-data
              - Sid: PlatformBackupAndMalwarePlan
                Effect: Allow
                Action:
                  - backup:*
                  - guardduty:*
                Resource: '*'
              - Sid: MountEncryptedBackupVault
                Effect: Allow
                Action:
                  - backup-storage:Mount
                  - backup-storage:MountCapsule
                Resource: '*'
              - Sid: GuardDutyServiceLinkedRole
                Effect: Allow
                Action: iam:CreateServiceLinkedRole
                Resource: '*'
                Condition:
                  StringEquals:
                    iam:AWSServiceName: guardduty.amazonaws.com
              - Sid: BackupServiceLinkedRole
                Effect: Allow
                Action: iam:CreateServiceLinkedRole
                Resource: !Sub 'arn:${{AWS::Partition}}:iam::${{AWS::AccountId}}:role/aws-service-role/backup.amazonaws.com/AWSServiceRoleForBackup'
                Condition:
                  StringEquals:
                    iam:AWSServiceName: backup.amazonaws.com
              - Sid: ApplyAwsServerlessTransform
                Effect: {platform_effect}
                Action: {platform_action}
                Resource: !Sub '{platform_resource}'{platform_duplicate}{platform_deny}
  IdpCloudFormationExecutionRole:
    Type: AWS::IAM::Role
    Properties:
      Policies:
        - PolicyName: IdpCloudFormation
          PolicyDocument:
            Statement:{idp_statement}
"""


def load_validator():
    path = Path(__file__).resolve().parents[1] / "scripts" / "validate-repository.py"
    spec = importlib.util.spec_from_file_location("repository_validator", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_workflow_trigger_names_supports_all_github_yaml_forms() -> None:
    validator = load_validator()
    path = Path("workflow.yml")

    assert validator.workflow_trigger_names("pull_request", path) == {"pull_request"}
    assert validator.workflow_trigger_names(["push", "workflow_dispatch"], path) == {
        "push",
        "workflow_dispatch",
    }
    assert validator.workflow_trigger_names(
        {"pull_request_target": {"types": ["opened"]}}, path
    ) == {"pull_request_target"}


@pytest.mark.parametrize("value", [None, 17, ["push", 17], {17: {}}])
def test_workflow_trigger_names_rejects_invalid_values(value: object) -> None:
    validator = load_validator()

    with pytest.raises(ValueError):
        validator.workflow_trigger_names(value, Path("workflow.yml"))


def test_serverless_transform_permission_is_scoped_to_each_execution_role() -> None:
    validator = load_validator()

    validator.validate_serverless_transform_execution_roles(bootstrap_transform_template())


def test_platform_cloudformation_handler_contract_is_exact() -> None:
    validator = load_validator()

    validator.validate_platform_cloudformation_handler_contract(
        platform_api_handler_template(), bootstrap_transform_template()
    )


def test_platform_packaging_contract_is_exact() -> None:
    validator = load_validator()
    script = """$packageArguments = @(
    '--s3-bucket', [string]$bootstrap.ArtifactBucketName,
    '--s3-prefix', "platform/$($config.environment)",
    '--kms-key-id', [string]$bootstrap.ArtifactKeyArn
)
"""

    validator.validate_platform_packaging_contract(script)


@pytest.mark.parametrize(
    "mutation",
    [
        ("ArtifactBucketName", "SourceBucketName"),
        ("platform/$($config.environment)", "platform/shared"),
        ("ArtifactKeyArn", "DataKeyArn"),
    ],
)
def test_platform_packaging_contract_rejects_coordinate_drift(
    mutation: tuple[str, str],
) -> None:
    validator = load_validator()
    script = """$packageArguments = @(
    '--s3-bucket', [string]$bootstrap.ArtifactBucketName,
    '--s3-prefix', "platform/$($config.environment)",
    '--kms-key-id', [string]$bootstrap.ArtifactKeyArn
)
"""

    with pytest.raises(ValueError, match="artifact bucket"):
        validator.validate_platform_packaging_contract(
            script.replace(mutation[0], mutation[1], 1)
        )


@pytest.mark.parametrize(
    ("api_template", "bootstrap_template"),
    [
        (
            platform_api_handler_template("generated-at-deploy"),
            bootstrap_transform_template(),
        ),
        (
            platform_api_handler_template(bucket_name="generated-at-deploy"),
            bootstrap_transform_template(),
        ),
        (
            platform_api_handler_template().replace("TableName: !Sub", "TableName:", 1),
            bootstrap_transform_template(),
        ),
        (
            platform_api_handler_template().replace("BucketName: !Sub", "BucketName:", 1),
            bootstrap_transform_template(),
        ),
        (
            platform_api_handler_template().replace("MaxLength: 13", "MaxLength: 64", 1),
            bootstrap_transform_template(),
        ),
        (
            platform_api_handler_template().replace(
                "Value: document-data", "Value: deployment-artifacts", 1
            ),
            bootstrap_transform_template(),
        ),
        (
            platform_api_handler_template(),
            bootstrap_transform_template().replace("MaxLength: 13", "MaxLength: 64", 1),
        ),
        (
            platform_api_handler_template(),
            bootstrap_transform_template().replace(
                "Value: deployment-artifacts", "Value: document-data", 1
            ),
        ),
        (
            platform_api_handler_template(),
            bootstrap_transform_template().replace(
                "-ci-artifacts-${AWS::AccountId}-${AWS::Region}",
                "-other-${AWS::AccountId}-${AWS::Region}",
                1,
            ),
        ),
        (
            platform_api_handler_template(),
            bootstrap_transform_template().replace(
                "BucketKeyEnabled: true", "BucketKeyEnabled: false", 1
            ),
        ),
        (
            platform_api_handler_template(),
            bootstrap_transform_template().replace(
                "KMSMasterKeyID: !GetAtt ArtifactKey.Arn",
                "KMSMasterKeyID: !GetAtt DataKey.Arn",
                1,
            ),
        ),
        (
            platform_api_handler_template(),
            bootstrap_transform_template().replace(
                "PlatformCloudFormationExecutionRole:\n    Type: AWS::IAM::Role",
                "PlatformCloudFormationExecutionRole:\n    Type: AWS::IAM::Policy",
                1,
            ),
        ),
        (
            platform_api_handler_template(),
            bootstrap_transform_template().replace("- W3037", "- W3037\n            - E3001", 1),
        ),
        (
            platform_api_handler_template(),
            bootstrap_transform_template().replace(
                "loan-document-${EnvironmentName}-registry/*'",
                "loan-document-${EnvironmentName}-other/*'",
                1,
            ),
        ),
        (
            platform_api_handler_template(),
            bootstrap_transform_template().replace(
                "loan-document-${EnvironmentName}-source-${AWS::AccountId}-${AWS::Region}/*'",
                "loan-document-${EnvironmentName}-other-${AWS::AccountId}-${AWS::Region}/*'",
                1,
            ),
        ),
        (
            platform_api_handler_template(),
            bootstrap_transform_template().replace(
                "- Sid: ExactPlatformResources\n                Effect: Allow",
                "- Sid: ExactPlatformResources\n                Effect: Deny",
                1,
            ),
        ),
        (
            platform_api_handler_template(),
            bootstrap_transform_template().replace(
                "                  - !Sub 'arn:${AWS::Partition}:dynamodb:",
                "                  - '*'\n"
                "                  - !Sub 'arn:${AWS::Partition}:dynamodb:",
                1,
            ),
        ),
        (
            platform_api_handler_template(),
            bootstrap_transform_template().replace(
                "${ArtifactBucket.Arn}/platform/${EnvironmentName}/*",
                "${ArtifactBucket.Arn}/platform/*",
                1,
            ),
        ),
        (
            platform_api_handler_template(),
            bootstrap_transform_template().replace(
                "Action: s3:GetObject",
                "Action:\n"
                "                  - s3:GetObject\n"
                "                  - s3:GetObjectVersion",
                1,
            ),
        ),
        (
            platform_api_handler_template(),
            bootstrap_transform_template().replace(
                "              - Sid: DecryptPlatformDeploymentArtifacts",
                "              - Sid: BlockArtifactRead\n"
                "                Effect: Deny\n"
                "                Action: s3:GetObject\n"
                "                Resource: '*'\n"
                "              - Sid: DecryptPlatformDeploymentArtifacts",
                1,
            ),
        ),
        (
            platform_api_handler_template(),
            bootstrap_transform_template().replace(
                "Resource: !GetAtt ArtifactKey.Arn",
                "Resource: '*'",
                1,
            ),
        ),
        (
            platform_api_handler_template(),
            bootstrap_transform_template().replace(
                "kms:EncryptionContext:aws:s3:arn: !GetAtt ArtifactBucket.Arn",
                "kms:EncryptionContext:aws:s3:arn: '*'",
                1,
            ),
        ),
        (
            platform_api_handler_template(),
            bootstrap_transform_template().replace(
                "kms:ViaService: !Sub 's3.${AWS::Region}.${AWS::URLSuffix}'",
                "kms:ViaService: '*'",
                1,
            ),
        ),
        (
            platform_api_handler_template(),
            bootstrap_transform_template().replace(
                "Action: kms:Decrypt\n                Resource: !GetAtt ArtifactKey.Arn",
                "Action: kms:*\n                Resource: !GetAtt ArtifactKey.Arn",
                1,
            ),
        ),
        (
            platform_api_handler_template(),
            bootstrap_transform_template().replace(
                "                    aws:RequestTag/KeyPurpose: document-data\n",
                "",
                1,
            ),
        ),
        (
            platform_api_handler_template(),
            bootstrap_transform_template().replace(
                "aws:ResourceTag/KeyPurpose: document-data",
                "aws:ResourceTag/KeyPurpose: deployment-artifacts",
                1,
            ),
        ),
        (
            platform_api_handler_template(),
            bootstrap_transform_template().replace(
                "              - Sid: PlatformBackupAndMalwarePlan",
                "              - Sid: BroadKmsDecryptBypass\n"
                "                Effect: Allow\n"
                "                Action: kms:Decrypt\n"
                "                Resource: '*'\n"
                "              - Sid: PlatformBackupAndMalwarePlan",
                1,
            ),
        ),
        (
            platform_api_handler_template(),
            bootstrap_transform_template().replace(
                "              - Sid: PlatformBackupAndMalwarePlan",
                "              - Sid: BroadS3Bypass\n"
                "                Effect: Allow\n"
                "                Action: s3:PutObject\n"
                "                Resource: '*'\n"
                "              - Sid: PlatformBackupAndMalwarePlan",
                1,
            ),
        ),
        (
            platform_api_handler_template(),
            bootstrap_transform_template()
            + "  AttachedInlineBypass:\n"
            "    Type: AWS::IAM::Policy\n"
            "    Properties:\n"
            "      Roles:\n"
            "        - !Ref PlatformCloudFormationExecutionRole\n"
            "      PolicyName: bypass\n"
            "      PolicyDocument:\n"
            "        Statement: []\n",
        ),
        (
            platform_api_handler_template(),
            bootstrap_transform_template()
            + "  AttachedManagedBypass:\n"
            "    Type: AWS::IAM::ManagedPolicy\n"
            "    Properties:\n"
            "      Roles:\n"
            "        - !Ref PlatformCloudFormationExecutionRole\n"
            "      PolicyDocument:\n"
            "        Statement: []\n",
        ),
        (
            platform_api_handler_template(),
            bootstrap_transform_template().replace(
                "backup-storage:Mount\n", "", 1
            ),
        ),
        (
            platform_api_handler_template(),
            bootstrap_transform_template().replace(
                "backup-storage:MountCapsule", "backup:CreateBackupVault", 1
            ),
        ),
        (
            platform_api_handler_template(),
            bootstrap_transform_template().replace(
                "backup-storage:Mount\n                  - backup-storage:MountCapsule",
                "backup-storage:*",
                1,
            ),
        ),
        (
            platform_api_handler_template(),
            bootstrap_transform_template().replace(
                "              - Sid: GuardDutyServiceLinkedRole",
                "              - Sid: BroadBackupStorageBypass\n"
                "                Effect: Allow\n"
                "                Action: backup-storage:*\n"
                "                Resource: '*'\n"
                "              - Sid: GuardDutyServiceLinkedRole",
                1,
            ),
        ),
        (
            platform_api_handler_template(),
            bootstrap_transform_template().replace(
                "backup-storage:MountCapsule\n                Resource: '*'",
                "backup-storage:MountCapsule\n"
                "                Resource: !Sub 'arn:${AWS::Partition}:backup:"
                "${AWS::Region}:${AWS::AccountId}:backup-vault:*'",
                1,
            ),
        ),
        (
            platform_api_handler_template(),
            bootstrap_transform_template().replace(
                "backup-storage:MountCapsule\n                Resource: '*'",
                "backup-storage:MountCapsule\n"
                "                Resource: '*'\n"
                "                Condition:\n"
                "                  Bool:\n"
                "                    aws:SecureTransport: true",
                1,
            ),
        ),
        (
            platform_api_handler_template(),
            bootstrap_transform_template().replace(
                "Properties:\n      Policies:",
                "Properties:\n"
                "      ManagedPolicyArns:\n"
                "        - arn:aws:iam::aws:policy/AdministratorAccess\n"
                "      Policies:",
                1,
            ),
        ),
        (
            platform_api_handler_template(),
            bootstrap_transform_template().replace(
                "Resource: !Sub 'arn:${AWS::Partition}:iam::${AWS::AccountId}:role/"
                "aws-service-role/backup.amazonaws.com/AWSServiceRoleForBackup'",
                "Resource: '*'",
                1,
            ),
        ),
        (
            platform_api_handler_template(),
            bootstrap_transform_template().replace(
                "iam:AWSServiceName: backup.amazonaws.com",
                "iam:AWSServiceName: ec2.amazonaws.com",
                1,
            ),
        ),
        (
            platform_api_handler_template(),
            bootstrap_transform_template().replace(
                "Action: iam:CreateServiceLinkedRole\n                Resource: !Sub",
                "Action: iam:*\n                Resource: !Sub",
                1,
            ),
        ),
        (
            platform_api_handler_template(),
            bootstrap_transform_template().replace(
                "              - Sid: ApplyAwsServerlessTransform",
                "              - Sid: BroadServiceLinkedRoleBypass\n"
                "                Effect: Allow\n"
                "                Action: iam:*\n"
                "                Resource: '*'\n"
                "              - Sid: ApplyAwsServerlessTransform",
                1,
            ),
        ),
        (
            platform_api_handler_template(),
            bootstrap_transform_template().replace(
                "iam:AWSServiceName: backup.amazonaws.com",
                "iam:AWSServiceName:\n"
                "                      - backup.amazonaws.com\n"
                "                      - ec2.amazonaws.com",
                1,
            ),
        ),
        (
            platform_api_handler_template(),
            bootstrap_transform_template().replace(
                "Resource: !Sub 'arn:${AWS::Partition}:iam::${AWS::AccountId}:role/"
                "aws-service-role/backup.amazonaws.com/AWSServiceRoleForBackup'",
                "Resource: 'arn:${AWS::Partition}:iam::${AWS::AccountId}:role/"
                "aws-service-role/backup.amazonaws.com/AWSServiceRoleForBackup'",
                1,
            ),
        ),
        (
            platform_api_handler_template(),
            bootstrap_transform_template().replace(
                "- Sid: BackupServiceLinkedRole\n                Effect: Allow",
                "- Sid: BackupServiceLinkedRole\n                Effect: Deny",
                1,
            ),
        ),
    ],
)
def test_platform_cloudformation_handler_contract_rejects_mutations(
    api_template: str, bootstrap_template: str
) -> None:
    validator = load_validator()

    with pytest.raises(
        ValueError,
        match=(
            "handler|service-linked|deterministic|authorized|execution role|"
            "inline role definitions|suppress|DataKey|Artifact|data-key|deployment|"
            "purpose-tagged"
        ),
    ):
        validator.validate_platform_cloudformation_handler_contract(
            api_template, bootstrap_template
        )


@pytest.mark.parametrize(
    "template",
    [
        bootstrap_transform_template(platform_effect="Deny"),
        bootstrap_transform_template(platform_action="cloudformation:*"),
        bootstrap_transform_template(platform_resource="*"),
        bootstrap_transform_template(deny_platform_transform=True),
        bootstrap_transform_template().replace(
            "Properties:\n      Policies:",
            "Properties:\n"
            "      PermissionsBoundary: arn:aws:iam::aws:policy/ReadOnlyAccess\n"
            "      Policies:",
            1,
        ),
        bootstrap_transform_template().replace(
            f"Resource: !Sub '{TRANSFORM_ARN}'",
            f"Resource: !Sub '{TRANSFORM_ARN}'\n"
            "                Condition:\n"
            "                  Bool:\n"
            "                    aws:SecureTransport: true",
            1,
        ),
        bootstrap_transform_template().replace(
            f"Resource: !Sub '{TRANSFORM_ARN}'",
            f"Resource:\n                  - !Sub '{TRANSFORM_ARN}'",
            1,
        ),
        bootstrap_transform_template(include_idp_statement=False),
        bootstrap_transform_template(
            include_idp_statement=False,
            duplicate_platform_statement=True,
        ),
        bootstrap_transform_template()
        + f"""  UnexpectedTransformPolicy:
    Type: AWS::IAM::ManagedPolicy
    Properties:
      PolicyDocument:
        Statement:
          - Effect: Allow
            Action: cloudformation:CreateChangeSet
            Resource: !Sub '{TRANSFORM_ARN}'
""",
        bootstrap_transform_template()
        + """  UnexpectedWildcardPolicy:
    Type: AWS::IAM::ManagedPolicy
    Properties:
      PolicyDocument:
        Statement:
          - Effect: Allow
            Action: cloudformation:*
            Resource: '*'
""",
        bootstrap_transform_template()
        + """  GitHubDeploymentRole:
    Type: AWS::IAM::Role
    Properties:
      ManagedPolicyArns:
        - arn:aws:iam::aws:policy/AdministratorAccess
""",
        bootstrap_transform_template()
        + """  UnexpectedConcreteTransformRole:
    Type: AWS::IAM::Role
    Properties:
      Policies:
        - PolicyName: UnexpectedTransformAccess
          PolicyDocument:
            Statement:
              - Effect: Allow
                Action: cloudformation:CreateChangeSet
                Resource: arn:aws:cloudformation:us-west-2:aws:transform/*
""",
        bootstrap_transform_template()
        + """  DeploymentUser:
    Type: AWS::IAM::User
    Properties:
      ManagedPolicyArns:
        - arn:aws:iam::aws:policy/AdministratorAccess
""",
        bootstrap_transform_template()
        + """  UnexpectedNotActionPolicy:
    Type: AWS::IAM::ManagedPolicy
    Properties:
      PolicyDocument:
        Statement:
          - Effect: Allow
            NotAction: s3:*
            Resource: '*'
""",
        bootstrap_transform_template()
        + """  UnexpectedNotResourcePolicy:
    Type: AWS::IAM::ManagedPolicy
    Properties:
      PolicyDocument:
        Statement:
          - Effect: Allow
            Action: cloudformation:CreateChangeSet
            NotResource: arn:aws:s3:::unrelated
""",
        bootstrap_transform_template()
        + """  UnexpectedConcreteNotResourceRole:
    Type: AWS::IAM::Role
    Properties:
      Policies:
        - PolicyName: UnexpectedInverseScope
          PolicyDocument:
            Statement:
              - Effect: Allow
                Action: cloudformation:CreateChangeSet
                NotResource: arn:aws-cn:cloudformation:cn-north-1:aws:transform/*
""",
        bootstrap_transform_template()
        + f"""  UnexpectedLiteralPseudoParameterRole:
    Type: AWS::IAM::Role
    Properties:
      Policies:
        - PolicyName: UnexpectedLiteralScope
          PolicyDocument:
            Statement:
              - Effect: Allow
                Action: cloudformation:CreateChangeSet
                NotResource: {TRANSFORM_ARN}
""",
        bootstrap_transform_template()
        + """  UnexpectedComposedActionPolicy:
    Type: AWS::IAM::ManagedPolicy
    Properties:
      PolicyDocument:
        Statement:
          - Effect: Allow
            Action:
              Fn::Join: ['', [cloudformation, ':*']]
            Resource: '*'
""",
        bootstrap_transform_template()
        + """  UnexpectedComposedResourcePolicy:
    Type: AWS::IAM::ManagedPolicy
    Properties:
      PolicyDocument:
        Statement:
          - Effect: Allow
            Action: cloudformation:CreateChangeSet
            Resource:
              Fn::Join: ['', ['*']]
""",
        bootstrap_transform_template()
        + """  UnexpectedConditionalEffectPolicy:
    Type: AWS::IAM::ManagedPolicy
    Properties:
      PolicyDocument:
        Statement:
          - Effect: !If [UseBroadPolicy, Allow, Deny]
            Action: cloudformation:CreateChangeSet
            Resource: '*'
""",
        bootstrap_transform_template()
        + """  UnexpectedSubActionPolicy:
    Type: AWS::IAM::ManagedPolicy
    Properties:
      PolicyDocument:
        Statement:
          - Effect: Allow
            Action: !Sub 'cloudformation:${UnresolvedAction}'
            Resource: '*'
""",
        bootstrap_transform_template()
        + """  UnexpectedSubResourcePolicy:
    Type: AWS::IAM::ManagedPolicy
    Properties:
      PolicyDocument:
        Statement:
          - Effect: Allow
            Action: cloudformation:CreateChangeSet
            Resource: !Sub '${UnresolvedArn}'
""",
        bootstrap_transform_template()
        + """  UnexpectedConditionalStatementPolicy:
    Type: AWS::IAM::ManagedPolicy
    Properties:
      PolicyDocument:
        Statement: !If
          - UseBroadPolicy
          - Effect: Allow
            Action: cloudformation:CreateChangeSet
            Resource: '*'
          - Effect: Deny
            Action: cloudformation:CreateChangeSet
            Resource: '*'
""",
        bootstrap_transform_template()
        + """  UnexpectedSubNotActionPolicy:
    Type: AWS::IAM::ManagedPolicy
    Properties:
      PolicyDocument:
        Statement:
          - Effect: Allow
            NotAction: !Sub '${UnresolvedAction}'
            Resource: '*'
""",
        bootstrap_transform_template()
        + """  UnexpectedSubNotResourcePolicy:
    Type: AWS::IAM::ManagedPolicy
    Properties:
      PolicyDocument:
        Statement:
          - Effect: Allow
            Action: cloudformation:CreateChangeSet
            NotResource: !Sub '${UnresolvedArn}'
""",
        bootstrap_transform_template(include_idp_statement=False)
        + f"# Resource: !Sub '{TRANSFORM_ARN}'\n",
    ],
)
def test_serverless_transform_permission_rejects_policy_mutations(template: str) -> None:
    validator = load_validator()

    with pytest.raises(ValueError, match="AWS Serverless transform"):
        validator.validate_serverless_transform_execution_roles(template)


def test_markdown_links_must_resolve_inside_repository(tmp_path: Path, monkeypatch) -> None:
    validator = load_validator()
    repository = tmp_path / "repository"
    docs = repository / "docs"
    docs.mkdir(parents=True)
    (repository / "README.md").write_text("# Repository\n", encoding="utf-8")
    outside = tmp_path / "outside.md"
    outside.write_text("# Outside\n", encoding="utf-8")
    markdown = docs / "guide.md"
    monkeypatch.setattr(validator, "ROOT", repository)

    markdown.write_text("[valid](../README.md)\n", encoding="utf-8")
    validator.validate_markdown_links(markdown)

    markdown.write_text("[escape](../../outside.md)\n", encoding="utf-8")
    with pytest.raises(ValueError, match="escapes the repository"):
        validator.validate_markdown_links(markdown)

    markdown.write_text(f"[absolute]({outside.as_posix()})\n", encoding="utf-8")
    with pytest.raises(ValueError, match="Absolute Markdown link"):
        validator.validate_markdown_links(markdown)

    encoded_path = docs / "%2e%2e" / "%2e%2e"
    encoded_path.mkdir(parents=True)
    (encoded_path / "outside.md").write_text("# Encoded path\n", encoding="utf-8")
    markdown.write_text(
        "[encoded escape](%2e%2e/%2e%2e/outside.md)\n", encoding="utf-8"
    )
    with pytest.raises(ValueError, match="escapes the repository"):
        validator.validate_markdown_links(markdown)


def test_active_feature_path_can_change_within_specs(tmp_path: Path, monkeypatch) -> None:
    validator = load_validator()
    repository = tmp_path / "repository"
    feature = repository / "specs" / "002-next-feature"
    feature.mkdir(parents=True)
    for name in ("spec.md", "plan.md", "tasks.md"):
        (feature / name).write_text(f"# {name}\n", encoding="utf-8")
    monkeypatch.setattr(validator, "ROOT", repository)

    resolved = validator.resolve_repository_path(
        repository, "specs/002-next-feature", "active feature path"
    )

    assert resolved == feature.resolve()
    assert resolved.is_relative_to((repository / "specs").resolve())


def test_azure_control_plane_rejects_an_aws_public_api(tmp_path: Path, monkeypatch) -> None:
    validator = load_validator()
    repository = tmp_path / "repository"
    files = {
        ".dockerignore": "**/.env\n**/*.pem\n**/*.key\n**/*.pfx\n**/*.pdf\n",
        ".specify/feature.json": '{"feature_directory":"specs/002-azure-api-control-plane"}',
        "vendor/idp.lock.json": '{"deploymentMode":"headless"}',
        "scripts/deploy-idp.ps1": (
            "idp-cli deploy --headless IdpCloudFormationExecutionRoleArn "
            "IdpRolePermissionsBoundaryArn PermissionsBoundaryArn= Set-AwsStatefulStackPolicy"
        ),
            "scripts/deploy-platform.ps1": (
                "PlatformCloudFormationExecutionRoleArn PlatformRolePermissionsBoundaryArn "
                "Set-AwsStatefulStackPolicy "
                "'--s3-bucket', [string]$bootstrap.ArtifactBucketName "
                "'--s3-prefix', \"platform/$($config.environment)\" "
                "'--kms-key-id', [string]$bootstrap.ArtifactKeyArn"
            ),
        "infra/api/template.yaml": (
            platform_api_handler_template()
            + "# EntraTenantOidcProvider:\n"
            "# AzureApiRuntimeRole:\n"
            "# RolePermissionsBoundaryArn:\n"
            + ("# PermissionsBoundary: !Ref RolePermissionsBoundaryArn\n" * 5)
            + "# sts:AssumeRoleWithWebIdentity\n"
            "# sts.windows.net/x/:aud\n"
            "# sts.windows.net/x/:sub\n"
            "# ${SourceBucket.Arn}/quarantine/tenants/*\n"
            "# prefix: quarantine/tenants/\n"
            "# UploadCompletionStreamMapping:\n"
            "# Type: AWS::Lambda::EventSourceMapping\n"
            "# EventSourceArn: !GetAtt RegistryTable.StreamArn\n"
            "# dynamodb:GetRecords\n"
            "# BisectBatchOnFunctionError: true\n"
            "# StartingPosition: TRIM_HORIZON\n"
            "# Destination: !GetAtt UploadProcessorDlq.Arn\n"
            "# ReportBatchItemFailures\n"
            "# AWS::ApiGatewayV2::Api"
        ),
        "infra/bootstrap/template.yaml": (
            bootstrap_transform_template()
            + "# PlatformRolePermissionsBoundary:\n# IdpRolePermissionsBoundary:\n"
            "# iam:PermissionsBoundary:\n# iam:PolicyARN:\n# iam:PassedToService:\n"
            "# DenyPlatformBoundaryRemoval\n# DenyIdpBoundaryRemoval\n"
            "# stack/${PlatformStackName}/*\n# stack/${IdpStackName}/*\n"
        ),
        "infra/stack-policies/protect-stateful-resources.json": (
            '{"Statement":[{"Effect":"Deny","Action":["Update:Delete","Update:Replace"],'
            '"Condition":{"StringEquals":{"ResourceType":["AWS::DynamoDB::Table",'
            '"AWS::KMS::Key","AWS::S3::Bucket"]}}}]}'
        ),
        "infra/azure/main.bicep": (
            "Microsoft.App/containerApps Microsoft.ManagedIdentity/userAssignedIdentities "
            "Microsoft.ContainerRegistry/registries Microsoft.Web/staticSites "
            "apiCustomDomainCertificateId customDomains: "
            "param maximumQueryItems int = 5000 "
            "param maximumLoanArchiveDocuments int = 500 "
            "param maximumLoanArchiveManifestBytes int = 4194304 "
            "name: 'MAXIMUM_QUERY_ITEMS' name: 'MAXIMUM_LOAN_ARCHIVE_DOCUMENTS' "
            "name: 'MAXIMUM_LOAN_ARCHIVE_MANIFEST_BYTES'"
        ),
        "infra/azure/acr-build-api.yml": (
            "version: v1.1.0\n"
            "env:\n"
            "  - DOCKER_BUILDKIT=1\n"
            "steps:\n"
            "  - build: --tag $Registry/{{.Values.image}} "
            "--file services/azure_api/Dockerfile .\n"
            "  - push:\n"
            "      - $Registry/{{.Values.image}}\n"
        ),
        "services/azure_api/main.py": "# runtime HOST_NOT_ALLOWED",
        "services/azure_api/auth.py": "# auth",
        "services/azure_api/aws_credentials.py": "# federation",
        "services/azure_api/settings.py": "# settings",
        "services/azure_api/Dockerfile": (
            "FROM python:3.13.14-slim-bookworm@sha256:"
            "9d7f287598e1a5a978c015ee176d8216435aaf335ed69ac3c38dd1bbb10e8d64\n"
            "RUN --mount=type=secret,id=enterprise_ca,required=false true\n"
            "USER 10001:10001\n"
            "CMD [\"uvicorn\", \"--no-access-log\"]\n"
        ),
        "services/loan_api/app.py": (
            'key = f"quarantine/tenants/{tenant}/source.pdf"\n'
            "connect_timeout=3 read_timeout=10 tcp_keepalive=True\n"
            'retries={"mode": "standard", "total_max_attempts": 3}\n'
            "MAXIMUM_QUERY_ITEMS MAXIMUM_LOAN_ARCHIVE_DOCUMENTS "
            "MAXIMUM_LOAN_ARCHIVE_MANIFEST_BYTES"
        ),
        "services/azure_api/requirements.txt": (
            "azure-identity==1.25.3\n"
            "boto3==1.43.51\n"
            "fastapi==0.139.1\n"
            "PyJWT[crypto]==2.13.0\n"
            "starlette==1.3.1\n"
            "uvicorn==0.51.0\n"
        ),
        "requirements-dev.txt": (
            "azure-identity==1.25.3\n"
            "boto3==1.43.51\n"
            "fastapi==0.139.1\n"
            "httpx2==2.7.0\n"
            "PyJWT[crypto]==2.13.0\n"
            "starlette==1.3.1\n"
        ),
        "scripts/deploy-azure.ps1": (
            "az acr run --file infra/azure/acr-build-api.yml "
            '--set "image=${ImageRepository}:$ImageTag" '
            "trivy image --severity HIGH,CRITICAL --ignore-unfixed "
            "--format cyclonedx # Production deployment cannot skip "
            "Get-LiveApiCustomDomainBinding dnsCutoverPerformed "
            "maximumQueryItems maximumLoanArchiveDocuments maximumLoanArchiveManifestBytes"
        ),
        "scripts/deploy-all.ps1": "deploy-azure.ps1 deploy-platform.ps1",
        "scripts/deploy-web.ps1": "az staticwebapp deploy",
        "scripts/cutover-api-domain.ps1": "azure.api.imageScan",
        "scripts/install-trivy.ps1": (
            "$version = '0.72.0'\n"
            "$expectedSha256 = 'bbb64b9695866ce4a7a8f5c9592002c5961cab378577fa3f8a040df362b9b2ea'\n"
            "github.com/aquasecurity/trivy/releases/download/v$version/$assetName\n"
            "Get-FileHash -LiteralPath $archivePath -Algorithm SHA256\n"
        ),
        "scripts/provision-entra-federation.ps1": "# provision",
        ".github/workflows/deploy-prod.yml": (
            "run: ./scripts/install-trivy.ps1\n"
        ),
        ".github/workflows/validate.yml": (
            "jobs:\n"
            "  validate:\n"
            "    steps:\n"
            "      - name: Build image\n"
            "        env:\n"
            "          DOCKER_BUILDKIT: '1'\n"
            "        run: docker build --file services/azure_api/Dockerfile .\n"
            "      - run: ./scripts/install-trivy.ps1\n"
            "      - run: >-\n"
            "          trivy image --scanners vuln --severity HIGH,CRITICAL "
            "--ignore-unfixed --exit-code 1 image\n"
            "      - run: trivy image --format cyclonedx image\n"
        ),
    }
    for relative_path, content in files.items():
        path = repository / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    monkeypatch.setattr(validator, "ROOT", repository)

    with pytest.raises(ValueError, match="Obsolete AWS public API surface"):
        validator.validate_azure_control_plane()

    template = repository / "infra" / "api" / "template.yaml"
    template.write_text(template.read_text(encoding="utf-8").replace("AWS::ApiGatewayV2::Api", ""), encoding="utf-8")
    validator.validate_azure_control_plane()

    acr_task_path = repository / "infra" / "azure" / "acr-build-api.yml"
    acr_task = acr_task_path.read_text(encoding="utf-8")
    acr_task_path.write_text(acr_task.replace("DOCKER_BUILDKIT=1", "DOCKER_BUILDKIT=0"), encoding="utf-8")
    with pytest.raises(ValueError, match="explicitly enable BuildKit"):
        validator.validate_azure_control_plane()
    acr_task_path.write_text(acr_task, encoding="utf-8")

    push_step = "  - push:\n      - $Registry/{{.Values.image}}\n"
    acr_task_path.write_text(acr_task.replace(push_step, ""), encoding="utf-8")
    with pytest.raises(ValueError, match="exactly one push step"):
        validator.validate_azure_control_plane()
    acr_task_path.write_text(acr_task, encoding="utf-8")

    workflow_path = repository / ".github" / "workflows" / "validate.yml"
    workflow = workflow_path.read_text(encoding="utf-8")
    workflow_path.write_text(workflow.replace("DOCKER_BUILDKIT: '1'", "DOCKER_BUILDKIT: '0'"), encoding="utf-8")
    with pytest.raises(ValueError, match="Docker builds must explicitly enable BuildKit"):
        validator.validate_azure_control_plane()
    workflow_path.write_text(workflow, encoding="utf-8")

    dockerfile_path = repository / "services" / "azure_api" / "Dockerfile"
    dockerfile = dockerfile_path.read_text(encoding="utf-8")
    dockerfile_path.write_text("# syntax=docker/dockerfile:1\n" + dockerfile, encoding="utf-8")
    with pytest.raises(ValueError, match="frontend must be pinned by immutable digest"):
        validator.validate_azure_control_plane()
    dockerfile_path.write_text(dockerfile, encoding="utf-8")

    deploy_path = repository / "scripts" / "deploy-azure.ps1"
    deploy_script = deploy_path.read_text(encoding="utf-8")
    deploy_path.write_text(deploy_script.replace("az acr run", "az acr build"), encoding="utf-8")
    with pytest.raises(ValueError, match="Exact-image production gate lacks"):
        validator.validate_azure_control_plane()
    deploy_path.write_text(deploy_script, encoding="utf-8")

    retired_edge = repository / "scripts" / "deploy-edge.ps1"
    retired_edge.write_text("# legacy", encoding="utf-8")
    with pytest.raises(ValueError, match="Retired AWS edge deployment source"):
        validator.validate_azure_control_plane()


def test_upload_completion_stream_mapping_is_durable_and_bounded() -> None:
    repository = Path(__file__).resolve().parents[1]
    template = (repository / "infra" / "api" / "template.yaml").read_text(encoding="utf-8")

    for required_fragment in (
        "StreamViewType: NEW_AND_OLD_IMAGES",
        "UploadCompletionStreamMapping:",
        "EventSourceArn: !GetAtt RegistryTable.StreamArn",
        "FunctionResponseTypes:\n        - ReportBatchItemFailures",
        "BisectBatchOnFunctionError: true",
        "MaximumRecordAgeInSeconds: 3600",
        "MaximumRetryAttempts: 5",
        "StartingPosition: TRIM_HORIZON",
        "Destination: !GetAtt UploadProcessorDlq.Arn",
        "dynamodb:DescribeStream",
        "dynamodb:GetRecords",
        "dynamodb:GetShardIterator",
        "dynamodb:ListStreams",
    ):
        assert required_fragment in template
