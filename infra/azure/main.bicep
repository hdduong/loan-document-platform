targetScope = 'resourceGroup'

@description('Short environment name used in tags and runtime configuration.')
@allowed([
  'dev'
  'test'
  'stage'
  'prod'
])
param environmentName string

@description('Azure region for all regional resources.')
param location string = resourceGroup().location

@description('Globally unique Azure Container Registry name.')
@minLength(5)
@maxLength(50)
param containerRegistryName string

@description('Azure Container Apps managed environment name.')
param containerAppsEnvironmentName string

@description('Azure Container App name for the public product API.')
param apiAppName string

@description('Stable user-assigned managed identity name for the API runtime.')
param apiManagedIdentityName string

@description('Azure Static Web App name reserved for the React SPA.')
param staticWebAppName string

@description('Deploy the API revision after federation and AWS runtime outputs exist.')
param deployApi bool = false

@description('Immutable ACR image reference. Required when deployApi is true.')
param containerImage string = ''

@description('FastAPI container listening port.')
@minValue(1024)
@maxValue(65535)
param apiTargetPort int = 8000

@description('Minimum API replicas. Production should use at least one.')
@minValue(0)
@maxValue(100)
param apiMinReplicas int = 1

@description('Maximum API replicas.')
@minValue(1)
@maxValue(300)
param apiMaxReplicas int = 10

@description('Concurrent HTTP requests per replica before scale-out.')
@minValue(1)
@maxValue(1000)
param concurrentRequestsPerReplica int = 1

@description('Log Analytics retention in days.')
@minValue(30)
@maxValue(730)
param logRetentionDays int = 90

@allowed([
  'Basic'
  'Standard'
  'Premium'
])
@description('Azure Container Registry SKU.')
param containerRegistrySku string = 'Basic'

@allowed([
  'Free'
  'Standard'
])
@description('Azure Static Web Apps SKU.')
param staticWebAppSku string = 'Free'

@description('Whether the Container Apps environment is distributed across availability zones.')
param containerAppsZoneRedundant bool = false

@description('Exact Entra tenant GUID for caller token validation.')
param entraTenantId string = ''

@description('Entra API application client ID used by Container Apps Easy Auth.')
param entraApiClientId string = ''

@description('Exact Entra API audience accepted by both Easy Auth and application validation.')
param entraApiAudience string = ''

@description('Exact SPA/service client IDs accepted by the product API.')
param allowedClientIds array = []

@description('Emergency-denied client IDs. Empty by default.')
param deniedClientIds array = []

@description('Exact browser origins accepted by ingress and FastAPI CORS.')
param allowedOrigins array = []

@description('Exact custom hostname accepted by production product routes.')
param apiHostName string = ''

@description('Existing Azure-managed certificate resource ID preserved on routine API revisions.')
param apiCustomDomainCertificateId string = ''

@description('AWS region containing the retained registry and document data plane.')
param awsRegion string = 'us-west-2'

@description('Dedicated Entra Application ID URI requested by managed identity for AWS federation.')
param awsFederationAudience string = ''

@description('Least-privilege AWS role assumed by the Azure API managed identity.')
param awsRoleArn string = ''

@description('Maximum AWS role session duration requested by the runtime.')
@minValue(900)
@maxValue(43200)
param awsSessionDurationSeconds int = 3600

@description('Seconds before AWS credential expiry when synchronized refresh begins.')
@minValue(60)
@maxValue(3600)
param awsCredentialRefreshSeconds int = 300

@description('Retained DynamoDB registry table name.')
param registryTableName string = ''

@description('Retained versioned S3 source/quarantine bucket name.')
param sourceBucketName string = ''

@description('Retained AWS KMS key ARN used by constrained S3 grants.')
param dataKeyArn string = ''

@description('Retained upload processor Lambda ARN used for bounded reconciliation.')
param uploadProcessorArn string = ''

@description('Maximum declared PDF upload size. Bytes never traverse this API.')
@minValue(1024)
param maximumUploadBytes int = 104857600

@description('Maximum accepted HTTP JSON request body size.')
@minValue(1024)
@maxValue(1048576)
param maximumRequestBodyBytes int = 65536

@description('Maximum extracted JSON size returned inline before a download grant is required.')
@minValue(1024)
@maxValue(20971520)
param maximumInlineDataPointsBytes int = 5242880

@description('Maximum DynamoDB records one product request may materialize from a partition query.')
@minValue(100)
@maxValue(100000)
param maximumQueryItems int = 5000

@description('Maximum number of logical documents allowed in one loan archive manifest.')
@minValue(1)
@maxValue(5000)
param maximumLoanArchiveDocuments int = 500

@description('Maximum serialized or downloaded loan archive manifest size in bytes.')
@minValue(1024)
@maxValue(20971520)
param maximumLoanArchiveManifestBytes int = 4194304

@description('Short-lived direct upload grant lifetime.')
@minValue(60)
@maxValue(3600)
param uploadUrlSeconds int = 600

@description('Short-lived exact-version download grant lifetime.')
@minValue(30)
@maxValue(900)
param downloadUrlSeconds int = 120

@description('Operations email receiver. Required when enableAlerts is true.')
param alertEmail string = ''

@description('Create Azure Monitor action group and API availability/latency alerts.')
param enableAlerts bool = false

@description('Monthly Azure resource-group budget amount in USD. Alerts are delayed notifications, not a hard stop.')
@minValue(1)
param monthlyBudgetUsd int = 100

@description('First day of a month, in YYYY-MM-DD form, from which the Azure budget applies.')
param budgetStartDate string

@description('Email receiver for actual and forecast Azure cost notifications.')
param budgetEmail string

@description('Additional non-sensitive Azure resource tags.')
param tags object = {}

var commonTags = union({
  Application: 'loan-document-platform'
  Environment: environmentName
  DataClassification: 'confidential-loan-document'
  ManagedBy: 'bicep'
}, tags)
var logWorkspaceName = take('${apiAppName}-logs', 63)
var applicationInsightsName = take('${apiAppName}-insights', 260)
var actionGroupName = take('${apiAppName}-operations', 260)
var actionGroupShortName = take(replace('${environmentName}loanops', '-', ''), 12)
var budgetName = take('loan-document-${environmentName}-monthly', 63)
var runtimeEnvironment = [
  {
    name: 'ENVIRONMENT_NAME'
    value: environmentName
  }
  {
    name: 'ENTRA_TENANT_ID'
    value: entraTenantId
  }
  {
    name: 'ENTRA_API_AUDIENCE'
    value: entraApiAudience
  }
  {
    name: 'ALLOWED_CLIENT_IDS'
    value: join(allowedClientIds, ',')
  }
  {
    name: 'DENIED_CLIENT_IDS'
    value: join(deniedClientIds, ',')
  }
  {
    name: 'REQUIRE_USER_ROLES'
    value: 'true'
  }
  {
    name: 'ALLOWED_ORIGINS'
    value: join(allowedOrigins, ',')
  }
  {
    name: 'API_HOST_NAME'
    value: apiHostName
  }
  {
    name: 'AZURE_MANAGED_IDENTITY_CLIENT_ID'
    value: apiRuntimeIdentity.properties.clientId
  }
  {
    name: 'AWS_FEDERATION_AUDIENCE'
    value: awsFederationAudience
  }
  {
    name: 'AWS_FEDERATION_SUBJECT'
    value: apiRuntimeIdentity.properties.principalId
  }
  {
    name: 'AWS_ROLE_ARN'
    value: awsRoleArn
  }
  {
    name: 'AWS_REGION'
    value: awsRegion
  }
  {
    name: 'AWS_SESSION_DURATION_SECONDS'
    value: string(awsSessionDurationSeconds)
  }
  {
    name: 'AWS_CREDENTIAL_REFRESH_SECONDS'
    value: string(awsCredentialRefreshSeconds)
  }
  {
    name: 'TABLE_NAME'
    value: registryTableName
  }
  {
    name: 'SOURCE_BUCKET'
    value: sourceBucketName
  }
  {
    name: 'DATA_KEY_ARN'
    value: dataKeyArn
  }
  {
    name: 'UPLOAD_PROCESSOR_ARN'
    value: uploadProcessorArn
  }
  {
    name: 'MAXIMUM_UPLOAD_BYTES'
    value: string(maximumUploadBytes)
  }
  {
    name: 'MAX_REQUEST_BODY_BYTES'
    value: string(maximumRequestBodyBytes)
  }
  {
    name: 'MAXIMUM_INLINE_DATA_POINTS_BYTES'
    value: string(maximumInlineDataPointsBytes)
  }
  {
    name: 'MAXIMUM_QUERY_ITEMS'
    value: string(maximumQueryItems)
  }
  {
    name: 'MAXIMUM_LOAN_ARCHIVE_DOCUMENTS'
    value: string(maximumLoanArchiveDocuments)
  }
  {
    name: 'MAXIMUM_LOAN_ARCHIVE_MANIFEST_BYTES'
    value: string(maximumLoanArchiveManifestBytes)
  }
  {
    name: 'UPLOAD_URL_SECONDS'
    value: string(uploadUrlSeconds)
  }
  {
    name: 'DOWNLOAD_URL_SECONDS'
    value: string(downloadUrlSeconds)
  }
  {
    name: 'JWKS_TIMEOUT_SECONDS'
    value: '5'
  }
  {
    name: 'IDP_DEPLOYMENT_MODE'
    value: 'headless'
  }
  {
    name: 'APPLICATIONINSIGHTS_CONNECTION_STRING'
    value: applicationInsights.properties.ConnectionString
  }
]

resource logWorkspace 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: logWorkspaceName
  location: location
  tags: commonTags
  properties: {
    retentionInDays: logRetentionDays
    sku: {
      name: 'PerGB2018'
    }
    features: {
      enableLogAccessUsingOnlyResourcePermissions: true
    }
    publicNetworkAccessForIngestion: 'Enabled'
    publicNetworkAccessForQuery: 'Enabled'
  }
}

resource applicationInsights 'Microsoft.Insights/components@2020-02-02' = {
  name: applicationInsightsName
  location: location
  tags: commonTags
  kind: 'web'
  properties: {
    Application_Type: 'web'
    Flow_Type: 'Bluefield'
    IngestionMode: 'LogAnalytics'
    Request_Source: 'rest'
    RetentionInDays: logRetentionDays
    WorkspaceResourceId: logWorkspace.id
    publicNetworkAccessForIngestion: 'Enabled'
    publicNetworkAccessForQuery: 'Enabled'
  }
}

resource apiRuntimeIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2024-11-30' = {
  name: apiManagedIdentityName
  location: location
  tags: commonTags
}

resource containerRegistry 'Microsoft.ContainerRegistry/registries@2023-07-01' = {
  name: containerRegistryName
  location: location
  tags: commonTags
  sku: {
    name: containerRegistrySku
  }
  properties: {
    adminUserEnabled: false
    dataEndpointEnabled: false
    networkRuleBypassOptions: 'AzureServices'
    policies: {
      exportPolicy: {
        status: 'enabled'
      }
      quarantinePolicy: {
        status: 'disabled'
      }
      retentionPolicy: {
        days: 7
        status: 'disabled'
      }
      trustPolicy: {
        status: 'disabled'
        type: 'Notary'
      }
    }
    publicNetworkAccess: 'Enabled'
    zoneRedundancy: containerRegistrySku == 'Premium' ? 'Enabled' : 'Disabled'
  }
}

resource containerAppsEnvironment 'Microsoft.App/managedEnvironments@2025-01-01' = {
  name: containerAppsEnvironmentName
  location: location
  tags: commonTags
  properties: {
    appLogsConfiguration: {
      destination: 'azure-monitor'
    }
    peerAuthentication: {
      mtls: {
        enabled: false
      }
    }
    peerTrafficConfiguration: {
      encryption: {
        enabled: true
      }
    }
    zoneRedundant: containerAppsZoneRedundant
  }
}

resource environmentDiagnostics 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {
  name: 'all-logs-and-metrics'
  scope: containerAppsEnvironment
  properties: {
    workspaceId: logWorkspace.id
    logAnalyticsDestinationType: 'Dedicated'
    logs: [
      {
        categoryGroup: 'allLogs'
        enabled: true
      }
    ]
    metrics: [
      {
        category: 'AllMetrics'
        enabled: true
      }
    ]
  }
}

resource staticWebApp 'Microsoft.Web/staticSites@2024-04-01' = {
  name: staticWebAppName
  location: location
  tags: commonTags
  sku: {
    name: staticWebAppSku
    tier: staticWebAppSku
  }
  properties: {
    allowConfigFileUpdates: true
    publicNetworkAccess: 'Enabled'
    stagingEnvironmentPolicy: environmentName == 'prod' ? 'Disabled' : 'Enabled'
  }
}

resource apiApp 'Microsoft.App/containerApps@2025-01-01' = if (deployApi) {
  name: apiAppName
  location: location
  tags: commonTags
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${apiRuntimeIdentity.id}': {}
    }
  }
  properties: {
    managedEnvironmentId: containerAppsEnvironment.id
    configuration: {
      activeRevisionsMode: 'Single'
      identitySettings: [
        {
          identity: apiRuntimeIdentity.id
          lifecycle: 'All'
        }
      ]
      ingress: {
        external: true
        allowInsecure: false
        clientCertificateMode: 'ignore'
        targetPort: apiTargetPort
        transport: 'auto'
        customDomains: empty(apiCustomDomainCertificateId) ? [] : [
          {
            name: apiHostName
            bindingType: 'SniEnabled'
            certificateId: apiCustomDomainCertificateId
          }
        ]
        corsPolicy: {
          allowCredentials: false
          allowedHeaders: [
            'Authorization'
            'Content-Type'
            'Idempotency-Key'
            'X-Correlation-Id'
          ]
          allowedMethods: [
            'GET'
            'POST'
            'OPTIONS'
          ]
          allowedOrigins: allowedOrigins
          exposeHeaders: [
            'X-Correlation-Id'
          ]
          maxAge: 300
        }
        traffic: [
          {
            latestRevision: true
            weight: 100
          }
        ]
      }
      maxInactiveRevisions: 5
      registries: [
        {
          server: containerRegistry.properties.loginServer
          identity: apiRuntimeIdentity.id
        }
      ]
    }
    template: {
      containers: [
        {
          name: 'api'
          image: containerImage
          env: runtimeEnvironment
          resources: {
            cpu: json('0.5')
            memory: '1Gi'
          }
          probes: [
            {
              type: 'Startup'
              httpGet: {
                path: '/health'
                port: apiTargetPort
                scheme: 'HTTP'
              }
              initialDelaySeconds: 1
              periodSeconds: 5
              timeoutSeconds: 2
              failureThreshold: 30
              successThreshold: 1
            }
            {
              type: 'Liveness'
              httpGet: {
                path: '/health'
                port: apiTargetPort
                scheme: 'HTTP'
              }
              initialDelaySeconds: 10
              periodSeconds: 10
              timeoutSeconds: 2
              failureThreshold: 3
              successThreshold: 1
            }
            {
              type: 'Readiness'
              httpGet: {
                path: '/ready'
                port: apiTargetPort
                scheme: 'HTTP'
              }
              initialDelaySeconds: 5
              periodSeconds: 5
              timeoutSeconds: 2
              failureThreshold: 3
              successThreshold: 1
            }
          ]
        }
      ]
      scale: {
        minReplicas: apiMinReplicas
        maxReplicas: apiMaxReplicas
        pollingInterval: 30
        cooldownPeriod: 300
        rules: [
          {
            name: 'http-concurrency'
            http: {
              metadata: {
                concurrentRequests: string(concurrentRequestsPerReplica)
              }
            }
          }
        ]
      }
    }
  }
}

resource apiAuth 'Microsoft.App/containerApps/authConfigs@2025-01-01' = if (deployApi) {
  parent: apiApp
  name: 'current'
  properties: {
    platform: {
      enabled: true
      runtimeVersion: '~1'
    }
    globalValidation: {
      excludedPaths: [
        '/health'
        '/ready'
      ]
      unauthenticatedClientAction: 'Return401'
    }
    httpSettings: {
      requireHttps: true
    }
    identityProviders: {
      azureActiveDirectory: {
        enabled: true
        registration: {
          clientId: entraApiClientId
          openIdIssuer: '${environment().authentication.loginEndpoint}${entraTenantId}/v2.0'
        }
        validation: {
          allowedAudiences: [
            entraApiAudience
          ]
          jwtClaimChecks: {
            allowedClientApplications: allowedClientIds
          }
        }
      }
    }
    login: {
      tokenStore: {
        enabled: false
      }
    }
  }
}

resource apiDiagnostics 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = if (deployApi) {
  name: 'all-metrics'
  scope: apiApp
  properties: {
    workspaceId: logWorkspace.id
    metrics: [
      {
        category: 'AllMetrics'
        enabled: true
      }
    ]
  }
}

resource resourceGroupBudget 'Microsoft.Consumption/budgets@2024-08-01' = {
  name: budgetName
  properties: {
    amount: monthlyBudgetUsd
    category: 'Cost'
    timeGrain: 'Monthly'
    timePeriod: {
      startDate: budgetStartDate
    }
    notifications: {
      actual_80_percent: {
        enabled: true
        operator: 'GreaterThanOrEqualTo'
        threshold: 80
        thresholdType: 'Actual'
        contactEmails: [
          budgetEmail
        ]
        contactGroups: []
        contactRoles: []
        locale: 'en-us'
      }
      forecast_100_percent: {
        enabled: true
        operator: 'GreaterThanOrEqualTo'
        threshold: 100
        thresholdType: 'Forecasted'
        contactEmails: [
          budgetEmail
        ]
        contactGroups: []
        contactRoles: []
        locale: 'en-us'
      }
    }
  }
}

resource operationsActionGroup 'Microsoft.Insights/actionGroups@2023-01-01' = if (enableAlerts) {
  name: actionGroupName
  location: 'global'
  tags: commonTags
  properties: {
    enabled: true
    groupShortName: actionGroupShortName
    emailReceivers: [
      {
        name: 'operations'
        emailAddress: alertEmail
        useCommonAlertSchema: true
      }
    ]
  }
}

resource apiServerErrorsAlert 'Microsoft.Insights/metricAlerts@2018-03-01' = if (deployApi && enableAlerts) {
  name: take('${apiAppName}-server-errors', 260)
  location: 'global'
  tags: commonTags
  properties: {
    description: 'Container Apps API returned more than five 5xx responses in five minutes.'
    severity: 1
    enabled: true
    autoMitigate: true
    evaluationFrequency: 'PT1M'
    windowSize: 'PT5M'
    scopes: [
      apiApp.id
    ]
    targetResourceType: 'Microsoft.App/containerApps'
    targetResourceRegion: location
    criteria: {
      'odata.type': 'Microsoft.Azure.Monitor.SingleResourceMultipleMetricCriteria'
      allOf: [
        {
          name: 'ServerErrors'
          criterionType: 'StaticThresholdCriterion'
          metricNamespace: 'Microsoft.App/containerApps'
          metricName: 'Requests'
          operator: 'GreaterThan'
          threshold: 5
          timeAggregation: 'Total'
          dimensions: [
            {
              name: 'statusCodeCategory'
              operator: 'Include'
              values: [
                '5xx'
              ]
            }
          ]
          skipMetricValidation: false
        }
      ]
    }
    actions: [
      {
        actionGroupId: operationsActionGroup.id
      }
    ]
  }
}

resource apiLatencyAlert 'Microsoft.Insights/metricAlerts@2018-03-01' = if (deployApi && enableAlerts) {
  name: take('${apiAppName}-latency', 260)
  location: 'global'
  tags: commonTags
  properties: {
    description: 'Container Apps API average response time exceeded two seconds for five minutes.'
    severity: 2
    enabled: true
    autoMitigate: true
    evaluationFrequency: 'PT1M'
    windowSize: 'PT5M'
    scopes: [
      apiApp.id
    ]
    targetResourceType: 'Microsoft.App/containerApps'
    targetResourceRegion: location
    criteria: {
      'odata.type': 'Microsoft.Azure.Monitor.SingleResourceMultipleMetricCriteria'
      allOf: [
        {
          name: 'ResponseTime'
          criterionType: 'StaticThresholdCriterion'
          metricNamespace: 'Microsoft.App/containerApps'
          metricName: 'ResponseTime'
          operator: 'GreaterThan'
          threshold: 2000
          timeAggregation: 'Average'
          dimensions: []
          skipMetricValidation: false
        }
      ]
    }
    actions: [
      {
        actionGroupId: operationsActionGroup.id
      }
    ]
  }
}

output apiManagedIdentityResourceId string = apiRuntimeIdentity.id
output apiManagedIdentityClientId string = apiRuntimeIdentity.properties.clientId
output apiManagedIdentityPrincipalId string = apiRuntimeIdentity.properties.principalId
output containerRegistryResourceId string = containerRegistry.id
output containerRegistryLoginServer string = containerRegistry.properties.loginServer
output containerAppsEnvironmentResourceId string = containerAppsEnvironment.id
output logAnalyticsWorkspaceResourceId string = logWorkspace.id
output applicationInsightsResourceId string = applicationInsights.id
output azureBudgetResourceId string = resourceGroupBudget.id
output staticWebAppResourceId string = staticWebApp.id
output staticWebAppDefaultHostname string = staticWebApp.properties.defaultHostname
output apiResourceId string = apiApp.?id ?? ''
output apiFqdn string = apiApp.?properties.configuration.ingress.fqdn ?? ''
output apiCustomDomainVerificationId string = apiApp.?properties.customDomainVerificationId ?? ''
