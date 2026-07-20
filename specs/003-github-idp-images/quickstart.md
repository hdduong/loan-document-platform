# Quickstart: Publish and Deploy GitHub-built IDP Images

This procedure assumes PowerShell 7, the configured `dev.json`, GitHub CLI
authenticated to `hdduong/aws-idp-custom-platform`, and the AWS/Azure bootstrap
already completed. It does not require Docker on the workstation.

## 1. Validate the repository

From the repository root, run:

```powershell
pwsh -NoProfile -File .\scripts\bootstrap.ps1 -EnvironmentFile .\config\environments\dev.json
python .\scripts\validate-repository.py
```

The bootstrap script must report the pinned IDP source and GitHub identity. It
must not write credentials or a filled environment file to Git.

## 2. Provision the image repository and publisher role

Run the existing GitHub provisioning script after reviewing the CloudFormation
change set:

```powershell
pwsh -NoProfile -File .\scripts\provision-github.ps1 -EnvironmentFile .\config\environments\dev.json
```

The script creates/updates the environment-scoped immutable ECR repository and
publisher OIDC role, then writes only non-secret GitHub environment variables.

## 3. Build the images in GitHub

Push the reviewed branch through a pull request. The PR workflow builds and
scans without AWS credentials or registry writes. After merge to `main`, start
the protected environment workflow from GitHub Actions for `dev`. The workflow
publishes the 15 images, waits for scans, creates attestations, and stores the
manifest as a GitHub artifact and in the bootstrap artifact bucket.

## 4. Deploy a manifest

Download the reviewed manifest artifact and run the manifest-driven deployment:

```powershell
pwsh -NoProfile -File .\scripts\deploy-idp.ps1 -EnvironmentFile .\config\environments\dev.json -ImageManifestFile .\.local\idp-image-release.json
```

The script verifies the AWS context, locked source, overlay checksum, exact
image set, ARM64 platform, scan/evidence state, and digest syntax before
CloudFormation changes anything. It packages templates and layers with the
pinned IDP CLI, but does not invoke Docker or CodeBuild.

## 5. Verify and reconcile

After deployment, inspect `.local/idp-dev.json` for the selected release ID,
manifest checksum, and non-secret IDP outputs. Run the existing platform
deployment/reconciliation script so the upload processor points at the new IDP
input/output buckets and KMS key. Exercise a synthetic document only.

## 6. Roll back

Select the previous complete manifest retained in the protected artifact bucket
and rerun step 4 with that file. The validator verifies that every referenced
digest still exists before any stack update. No source checkout or image build
is required for rollback.
