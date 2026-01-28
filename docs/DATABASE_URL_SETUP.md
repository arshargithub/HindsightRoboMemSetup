# Database URL Setup for Hindsight

Hindsight expects `HINDSIGHT_API_DATABASE_URL` as a full PostgreSQL connection string:
```
postgresql://username:password@host:port/dbname
```

## Current CDK Implementation

The CDK stack creates a `DbUrlSecret` in Secrets Manager. The **GitHub Actions workflow** automatically constructs and populates this secret after CDK deployment.

## Automatic Setup (via GitHub Actions)

The workflow includes a **"Set Database URL Secret"** step that:
1. Gets the RDS endpoint from CDK outputs
2. Gets the RDS password from Secrets Manager
3. Constructs the full connection string
4. Updates the `DbUrlSecret` in Secrets Manager

This happens automatically after each CDK deploy, so you don't need to do anything manually.

## Manual Setup (if needed)

If the automatic step fails, you can manually set the database URL:

```bash
# Get RDS endpoint and password
RDS_ENDPOINT=$(aws cloudformation describe-stacks \
  --stack-name HindsightDevStack \
  --query 'Stacks[0].Outputs[?OutputKey==`RdsEndpoint`].OutputValue' \
  --output text)

RDS_PASSWORD=$(aws secretsmanager get-secret-value \
  --secret-id <RdsSecretArn> \
  --query 'SecretString' \
  --output text | jq -r '.password')

# Construct URL
DB_URL="postgresql://hindsight_admin:${RDS_PASSWORD}@${RDS_ENDPOINT}:5432/hindsight"

# Get DB URL secret ARN from CDK outputs
DB_URL_SECRET_ARN=$(aws cloudformation describe-stacks \
  --stack-name HindsightDevStack \
  --query 'Stacks[0].Outputs[?OutputKey==`DbUrlSecretArn`].OutputValue' \
  --output text)

# Update secret
aws secretsmanager put-secret-value \
  --secret-id "$DB_URL_SECRET_ARN" \
  --secret-string "$DB_URL"
```

## Verification

After deployment, verify the secret is set:

```bash
aws secretsmanager get-secret-value \
  --secret-id <DbUrlSecretArn> \
  --query 'SecretString' \
  --output text
```

You should see a connection string like:
```
postgresql://hindsight_admin:password@hindsight-db.xxxxx.ca-central-1.rds.amazonaws.com:5432/hindsight
```
