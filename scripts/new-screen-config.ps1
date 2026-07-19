[CmdletBinding()]
param(
    [string]$FullConfigPath = (Join-Path $PSScriptRoot '..\config\idp\cd-full-v1.json'),
    [string]$OutputPath = (Join-Path $PSScriptRoot '..\config\idp\cd-screen-v1.json')
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
Import-Module (Join-Path $PSScriptRoot 'common.psm1') -Force

$fullConfigPath = (Resolve-Path -LiteralPath $FullConfigPath).Path
$config = Get-Content -Raw -LiteralPath $fullConfigPath | ConvertFrom-Json -Depth 100

$config.managed = $false
$config.notes = @"
Closing Disclosure package screening configuration. Textract features are intentionally empty,
which makes GenAI IDP v0.5.16 use DetectDocumentText rather than AnalyzeDocument. Every package
page is classified because CD boundaries are unknown before submission. Only the small identity,
date, signature, and corrected/final evidence schema is extracted. Full business extraction is
performed later with cd-full-v1 on the selected 5-6 pages.
"@.Trim()

# Empty features is the upstream v0.5.16 switch to Textract DetectDocumentText.
$config.ocr.features = @()
$config.ocr.max_workers = '20'

$config.classification.model = 'us.amazon.nova-2-lite-v1:0'
$config.classification.maxPagesForClassification = 'ALL'
$config.classification.classificationMethod = 'multimodalPageLevelClassification'
$config.classification.sectionSplitting = 'llm_determined'
$config.classification.contextPagesCount = '1'
$config.classification.temperature = '0.0'
$config.classification.top_p = '0.1'
$config.classification.top_k = '5.0'
$config.classification.max_tokens = '512'

$config.extraction.model = 'us.amazon.nova-2-lite-v1:0'
$config.extraction.system_prompt = @'
You extract only screening evidence from a borrower Closing Disclosure section. Return valid JSON
matching the supplied schema. Never invent values. Use null when evidence is blank, absent,
illegible, or uncertain. Preserve printed identifiers and normalize unambiguous dates to
YYYY-MM-DD. Corrected/PCCD/Final status must be based on explicit printed evidence; do not infer
status solely from a recent date or a signature.
'@.Trim()
$config.extraction.task_prompt = @'
Extract the small screening schema from this {DOCUMENT_CLASS} section.

<field-definitions>
{ATTRIBUTE_NAMES_AND_DESCRIPTIONS}
</field-definitions>

<document-image>
{DOCUMENT_IMAGE}
</document-image>

<document-text>
{DOCUMENT_TEXT}
</document-text>

Return only valid JSON matching the schema. Use null for uncertain or absent evidence.
'@.Trim()
$config.extraction.temperature = '0.0'
$config.extraction.top_p = '0.1'
$config.extraction.top_k = '5.0'
$config.extraction.max_tokens = '2048'
$config.extraction.agentic.enabled = $false
$config.extraction.agentic.review_agent = $false

$config.assessment.enabled = $false
$config.assessment.hitl_enabled = $false
$config.summarization.enabled = $false
$config.rule_validation.enabled = $false
$config.evaluation.enabled = $false

$closingClass = $config.classes | Where-Object { $_.'$id' -eq 'L053_Closing_Disclosure' }
if ($null -eq $closingClass) {
    throw 'The full configuration does not contain L053_Closing_Disclosure.'
}

$closingClass.description = 'Borrower Closing Disclosure main form pages. Extract only the evidence required to select a unique final/corrected CD.'
$closingClass.'x-classification-only' = $false
$closingClass.PSObject.Properties.Remove('x-aws-idp-exclude-from-processing')
$closingClass.PSObject.Properties.Remove('x-aws-idp-exclusion-reason')
$closingClass.properties = [ordered]@{
    LoanIdentifier = [ordered]@{
        type = @('string', 'null')
        description = "Loan ID # printed in the Page 1 Loan Information section. Preserve the printed value; do not infer it from package metadata."
    }
    PrimaryBorrowerName = [ordered]@{
        type = @('string', 'null')
        description = "Primary borrower name printed in the Page 1 Transaction Information section next to Borrower."
    }
    CoBorrowerName = [ordered]@{
        type = @('string', 'null')
        description = "Second borrower name printed next to Borrower. Return null when no co-borrower is printed."
    }
    PropertyAddress = [ordered]@{
        type = @('string', 'null')
        description = "Complete subject property address printed in Page 1 Closing Information, combining both visible lines without inventing missing parts."
    }
    DateIssued = [ordered]@{
        type = @('string', 'null')
        description = "Date Issued from Page 1. Normalize an unambiguous date to YYYY-MM-DD."
    }
    ClosingDate = [ordered]@{
        type = @('string', 'null')
        description = "Closing Date from Page 1 Closing Information. Normalize an unambiguous date to YYYY-MM-DD."
    }
    DisbursementDate = [ordered]@{
        type = @('string', 'null')
        description = "Disbursement Date from Page 1 Closing Information. Normalize an unambiguous date to YYYY-MM-DD."
    }
    BorrowerSignaturePresent = [ordered]@{
        type = @('boolean', 'null')
        description = "True only when the primary borrower signature is visibly present in Confirm Receipt; false when the signature line is visibly blank; null when unavailable or uncertain."
    }
    BorrowerExecutionDate = [ordered]@{
        type = @('string', 'null')
        description = "Date beside the primary borrower signature. Normalize an unambiguous date to YYYY-MM-DD."
    }
    CoBorrowerSignaturePresent = [ordered]@{
        type = @('boolean', 'null')
        description = "True only when a co-borrower signature is visibly present; false when the line is visibly blank; null when no co-borrower or uncertain."
    }
    CoBorrowerExecutionDate = [ordered]@{
        type = @('string', 'null')
        description = "Date beside the co-borrower signature. Normalize an unambiguous date to YYYY-MM-DD."
    }
    DocumentVariant = [ordered]@{
        type = @('string', 'null')
        enum = @('PCCD', 'CORRECTED', 'FINAL', 'UNKNOWN', $null)
        description = "Return PCCD, CORRECTED, or FINAL only when that status has explicit printed evidence. Otherwise return UNKNOWN. Do not classify status from date recency or signature alone."
    }
    VariantEvidenceText = [ordered]@{
        type = @('string', 'null')
        description = "Short exact printed phrase supporting DocumentVariant. Return null when no explicit status phrase is present."
    }
}

if ($null -eq $closingClass.PSObject.Properties['required']) {
    $closingClass | Add-Member -NotePropertyName 'required' -NotePropertyValue @()
} else {
    $closingClass.required = @()
}
$closingClass.'x-aws-idp-examples' = @()

# Classification-only classes remain excluded from extraction.
foreach ($class in $config.classes) {
    if ($class.'$id' -ne 'L053_Closing_Disclosure') {
        $class.'x-classification-only' = $true
        if ($null -eq $class.PSObject.Properties['x-aws-idp-exclude-from-processing']) {
            $class | Add-Member -NotePropertyName 'x-aws-idp-exclude-from-processing' -NotePropertyValue $true
        } else {
            $class.'x-aws-idp-exclude-from-processing' = $true
        }
        $class.properties = [ordered]@{}
    }
}

$config._config_format = 'full'
$outputDirectory = Split-Path -Parent $OutputPath
if (-not (Test-Path -LiteralPath $outputDirectory)) {
    New-Item -ItemType Directory -Path $outputDirectory -Force | Out-Null
}

$json = $config | ConvertTo-Json -Depth 100
[System.IO.File]::WriteAllText($OutputPath, $json + [Environment]::NewLine, [System.Text.UTF8Encoding]::new($false))

$screenHash = Get-NormalizedTextSha256 -Path $OutputPath
$fullHash = Get-NormalizedTextSha256 -Path $fullConfigPath
Write-Host "Wrote $OutputPath"
Write-Host "cd-screen-v1 sha256: $screenHash"
Write-Host "cd-full-v1   sha256: $fullHash"
