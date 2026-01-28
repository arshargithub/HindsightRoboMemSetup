# Setting Up AWS OIDC for GitHub Actions

This guide walks you through configuring **OIDC** (OpenID Connect) so GitHub Actions can deploy to AWS without storing long-lived access keys.

---

## Overview

**OIDC** allows GitHub Actions to assume an **IAM role** in AWS using short-lived credentials. No access keys stored in GitHub Secrets.

**Flow:**
1. GitHub Actions workflow runs
2. GitHub's OIDC provider issues a token
3. Workflow assumes AWS IAM role using that token
4. Workflow gets temporary AWS credentials (expire in ~1 hour)
5. Workflow runs CDK/Terraform, deploys to ECS, etc.

---

## Step 1: Create OIDC Identity Provider in AWS

1. **Go to IAM Console** → **Identity providers** → **Add provider**

2. **Provider type**: **OpenID Connect**

3. **Provider URL**: `https://token.actions.githubusercontent.com`

4. **Audience**: `sts.amazonaws.com`  
   - AWS may show generic text about "registering your app with the IdP" to get a client ID. **Ignore that** for GitHub Actions. You don’t register a custom app. `sts.amazonaws.com` is the standard audience for the [GitHub Actions ↔ AWS OIDC integration](https://docs.github.com/en/actions/deployment/security-hardening-your-deployments/configuring-openid-connect-in-amazon-web-services). The `aws-actions/configure-aws-credentials` action requests tokens with this audience, and AWS STS accepts them.

5. **Click "Add provider"**

---

## Step 2: Create IAM Roles (Dev and Prod)

You'll create **two IAM roles** (one for dev, one for prod) that GitHub Actions can assume.

AWS may show a **simplified GitHub form** with: Identity provider, Audience, **GitHub organization**, **GitHub repository** (optional), **GitHub branch** (optional). Use the values below. If you see a generic "Add condition" flow instead, use the **Manual trust policy** section at the end.

### Create Dev Role

1. **IAM Console** → **Roles** → **Create role**

2. **Trusted entity type**: **Web identity**

3. **Identity provider**: Select `token.actions.githubusercontent.com` (the one you created in Step 1)

4. **Audience**: `sts.amazonaws.com`

5. **GitHub organization**: `arshargithub` (your GitHub username; use your org name if the repo is under an org)

6. **GitHub repository**: `HindsightRoboMemSetup`

7. **GitHub branch**: `main`

8. **Click "Next"**

9. **Permissions**: Attach a policy (or create custom). The role needs permissions for:
   - VPC, EC2 (for VPC, subnets, NAT)
   - RDS (create/update databases)
   - ECS, ECR, ALB (for container deployment)
   - Lambda, EventBridge (for Reflect scheduler)
   - Secrets Manager (read/write secrets)
   - IAM (to create roles for ECS tasks, Lambda)
   - CloudFormation (CDK uses it)
   - CloudWatch Logs

   **Quick option**: Attach `AdministratorAccess` for now (for testing). **Later**, create a least-privilege policy.

10. **Role name**: `GitHubActionsHindsightDev`

11. **Click "Create role"**

12. **Copy the Role ARN** (e.g. `arn:aws:iam::123456789012:role/GitHubActionsHindsightDev`)

13. **Verify trust policy** (optional but recommended): IAM → Roles → `GitHubActionsHindsightDev` → **Trust relationships**. Check that the `sub` condition is `repo:arshargithub/HindsightRoboMemSetup:ref:refs/heads/main`. Fix if not.

### Create Prod Role

1. Same as dev: **Create role** → **Web identity** → select the GitHub OIDC provider.

2. **Audience**: `sts.amazonaws.com`

3. **GitHub organization**: `arshargithub`

4. **GitHub repository**: `HindsightRoboMemSetup`

5. **GitHub branch**: **Leave blank** (prod deploys from **tags/releases**, not a branch). The UI only has "branch"; leaving it blank usually allows any ref (including tags).

6. **Click "Next"**

7. **Permissions**: Attach the same policy as dev (e.g. `AdministratorAccess`).

8. **Role name**: `GitHubActionsHindsightProd`

9. **Click "Create role"**

10. **Copy the Role ARN**.

11. **Verify trust policy**: IAM → Roles → `GitHubActionsHindsightProd` → **Trust relationships** → **Edit**. The `sub` condition should allow tags (e.g. `repo:arshargithub/HindsightRoboMemSetup:ref:refs/tags/*`). If it only allows `refs/heads/main`, **edit** it to use `ref:refs/tags/*` so only releases can assume the prod role.

### Manual trust policy (if you don't see the GitHub form)

If you see a generic **Add condition** flow instead of Organization/Repo/Branch:

- **Condition key**: `token.actions.githubusercontent.com:sub`
- **Dev value**: `repo:arshargithub/HindsightRoboMemSetup:ref:refs/heads/main`
- **Prod value**: `repo:arshargithub/HindsightRoboMemSetup:ref:refs/tags/*`

---

## Step 3: Add GitHub Secrets

Go to your GitHub repo → **Settings** → **Secrets and variables** → **Actions** → **New repository secret**

Add these secrets:

| Secret Name | Value | Notes |
|-------------|-------|-------|
| `AWS_ROLE_ARN_DEV` | `arn:aws:iam::ACCOUNT-ID:role/GitHubActionsHindsightDev` | Dev IAM role ARN |
| `AWS_ROLE_ARN_PROD` | `arn:aws:iam::ACCOUNT-ID:role/GitHubActionsHindsightProd` | Prod IAM role ARN |
| `LLM_API_KEY` | Your LLM provider API key (OpenAI, Groq, etc.) | Must match the provider in `cdk/config.py` |

**Note**: Provider and model are **not** in GitHub Secrets. They’re set in **`cdk/config.py`** (`LLM_PROVIDER`, `LLM_MODEL`). The workflow gets `LlmSecretArn` from **stack outputs after deploy** (you can't use outputs before the stack has finished), then writes `LLM_API_KEY` into that secret and forces an ECS redeploy. You do **not** need `LLM_SECRET_ARN_DEV` / `LLM_SECRET_ARN_PROD` in GitHub.


---

## Step 4: First Deploy (Manual)

The workflow uses **stack outputs only after the stack has finished** (you can't get outputs beforehand). It gets `LlmSecretArn` from outputs, writes `LLM_API_KEY` into that secret, then forces an ECS redeploy. You only need `LLM_API_KEY` (and the AWS role ARNs) in GitHub Secrets.

Before the workflow can deploy, you need to:

1. **Configure AWS credentials via IAM Identity Center (SSO)** (required for bootstrap and first deploy):

   **Prerequisite:** IAM Identity Center must be enabled and you must have a permission set assigned for your account (e.g. PowerUser or a custom policy for CDK/ECS/RDS).

   ```bash
   aws configure sso
   ```
   Enter your SSO start URL, region (`ca-central-1`), and the profile name you want (e.g. `hindsight-sso`). When prompted, choose your account and permission set.

   Then sign in (run this whenever your SSO session expires, e.g. every 8–12 hours):
   ```bash
   aws sso login --profile hindsight-sso
   ```
   Verify with `aws sts get-caller-identity --profile hindsight-sso`.

   **Using the profile with CDK:** Either set `export AWS_PROFILE=hindsight-sso` before bootstrap/deploy, or pass `--profile hindsight-sso` to `npx aws-cdk` commands (e.g. `npx aws-cdk bootstrap ... --profile hindsight-sso`).

2. **Bootstrap CDK** (one-time per account/region):
   ```bash
   cd cdk
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   npx aws-cdk bootstrap aws://ACCOUNT-ID/ca-central-1 --profile hindsight-sso
   ```
   Use `npx aws-cdk` (no global npm install). Replace `ACCOUNT-ID` with your AWS account ID. Use `--profile hindsight-sso` (or your SSO profile name), or ensure `AWS_PROFILE` is set.

3. **Deploy dev stack manually** (first time only, so the stack exists before the workflow runs):
   ```bash
   npx aws-cdk deploy HindsightDevStack --profile hindsight-sso
   ```
   After this, **push to `main`** (or use workflow_dispatch). The workflow will deploy, read `LlmSecretArn` from stack outputs, store `LLM_API_KEY` in Secrets Manager, and force an ECS redeploy so tasks use the key.

4. **Repeat for prod** (deploy prod stack manually once, then use releases / workflow_dispatch for prod).

---

## Step 5: Test the Workflow

1. **Push to `main`** → Should trigger **dev deploy**
2. **Create a GitHub Release** (tag) → Should trigger **prod deploy**

---

## Troubleshooting

### "Not authorized to perform sts:AssumeRoleWithWebIdentity"

- Check the **IAM role trust policy** includes the correct `sub` condition
- Verify the **OIDC provider** is configured correctly
- Check the **GitHub repo name** matches exactly in the condition

### "Secret not found" in Secrets Manager

- The workflow gets `LlmSecretArn` from stack outputs **after** deploy. Ensure the stack has finished successfully before the "Set LLM API key" step runs.

### CDK bootstrap fails

- Ensure you have signed in via SSO (`aws sso login --profile hindsight-sso`) and use that profile for CDK (`--profile hindsight-sso` or `AWS_PROFILE`)
- Check your IAM Identity Center permission set allows CloudFormation, S3, IAM, etc. (or use PowerUser/AdministratorAccess for bootstrap)

### ECS tasks fail with "Incorrect API key" (401 errors)

- **Root cause**: The `LlmApiKeySecret` is created empty and must be populated before ECS tasks can start successfully.
- **Solution**: 
  - GitHub Actions workflow deploys, then reads `LlmSecretArn` from stack outputs, stores `LLM_API_KEY` in Secrets Manager, and forces an ECS redeploy so new tasks get the key.
  - For manual deployments, populate it manually:
    ```bash
    # Get the secret ARN from CDK outputs
    aws cloudformation describe-stacks \
      --stack-name HindsightDevStack \
      --region ca-central-1 \
      --query 'Stacks[0].Outputs[?OutputKey==`LlmSecretArn`].OutputValue' \
      --output text
    
    # Update the secret
    aws secretsmanager put-secret-value \
      --secret-id <secret-arn> \
      --secret-string <your-openai-api-key> \
      --region ca-central-1
    ```
  - After updating, force ECS to restart tasks:
    ```bash
    aws ecs update-service \
      --cluster <cluster-name> \
      --service <service-name> \
      --force-new-deployment \
      --region ca-central-1
    ```

### ECS tasks fail with database connection timeout

- **Root cause**: Security group rule allowing ECS → RDS on port 5432 may be missing.
- **Solution**: The CDK code now automatically adds this rule. If deploying an existing stack, update it or manually add the rule:
  ```bash
  # Get security group IDs
  RDS_SG=$(aws cloudformation describe-stack-resources --stack-name HindsightDevStack --query 'StackResources[?LogicalResourceId==`RdsInstance`].PhysicalResourceId' --output text | xargs aws rds describe-db-instances --db-instance-identifier --query 'DBInstances[0].VpcSecurityGroups[0].VpcSecurityGroupId' --output text)
  ECS_SG=$(aws cloudformation describe-stack-resources --stack-name HindsightDevStack --query 'StackResources[?contains(LogicalResourceId, `HindsightApiServiceSecurityGroup`) && ResourceType==`AWS::EC2::SecurityGroup`].PhysicalResourceId' --output text)
  
  # Add ingress rule
  aws ec2 authorize-security-group-ingress \
    --group-id $RDS_SG \
    --protocol tcp \
    --port 5432 \
    --source-group $ECS_SG \
    --region ca-central-1
  ```

### Stack deletion stuck on PgvectorEnabler / Lambda (ENI or security group)

Stack delete can hang on `PgvectorEnabler` or `PgvectorLambdaSecurityGroup` for two reasons:

**1. Lambda-in-VPC ENI cleanup**  
The PgvectorEnabler Lambda runs in a VPC. AWS deletes its ENIs only after the Lambda is removed; that can take **up to ~45 minutes**. CloudFormation waits on it.

- **Option A**: Wait up to 45 minutes and leave the delete running.
- **Option B**: If the stack eventually goes to **DELETE_FAILED** (e.g. timeout), retry delete and **retain** the stuck resources:
  1. CloudFormation → your stack → **Delete**.
  2. When prompted for “resources to retain”, select **PgvectorEnabler** (and **PgvectorLambdaSecurityGroup** if that’s also stuck).
  3. Complete the delete. The stack goes to `DELETE_COMPLETE`; the retained Lambda (and SG) remain.
  4. Delete the Lambda (and SG) manually later once ENIs have cleared, or leave them if you’ll redeploy.

  You can only choose “resources to retain” when **retrying** a delete after **DELETE_FAILED**. While the stack is **DELETE_IN_PROGRESS**, you must either wait or let it time out.

**2. Security group dependency**  
RDS has an ingress rule “allow from PgvectorLambdaSecurityGroup”. That blocks deletion of the Lambda’s security group.

- **Fix**: Revoke that rule, then retry stack delete (or delete the SG manually if you retained it):
  ```bash
  REGION=ca-central-1
  PROFILE=hindsight-sso

  # RDS instance ID and its security group (from stack; skip if RDS already deleted)
  RDS_ID=$(aws cloudformation describe-stack-resources --stack-name HindsightDevStack --region $REGION --profile $PROFILE \
    --query 'StackResources[?LogicalResourceId==`RdsInstance`].PhysicalResourceId' --output text 2>/dev/null)
  RDS_SG=""
  if [ -n "$RDS_ID" ] && [ "$RDS_ID" != "None" ]; then
    RDS_SG=$(aws rds describe-db-instances --db-instance-identifier "$RDS_ID" --region $REGION --profile $PROFILE \
      --query 'DBInstances[0].VpcSecurityGroups[0].VpcSecurityGroupId' --output text 2>/dev/null)
  fi

  # Pgvector Lambda security group (from stack resources)
  PGVECTOR_SG=$(aws cloudformation describe-stack-resources --stack-name HindsightDevStack --region $REGION --profile $PROFILE \
    --query 'StackResources[?contains(LogicalResourceId, `PgvectorLambda`) && ResourceType==`AWS::EC2::SecurityGroup`].PhysicalResourceId' --output text | head -1)

  if [ -n "$RDS_SG" ] && [ -n "$PGVECTOR_SG" ]; then
    aws ec2 revoke-security-group-ingress --group-id $RDS_SG --protocol tcp --port 5432 --source-group $PGVECTOR_SG --region $REGION --profile $PROFILE
    echo "Revoked RDS SG rule allowing from Pgvector Lambda SG. Retry stack delete."
  else
    echo "Could not resolve RDS_SG or PGVECTOR_SG. Check stack/region/profile."
  fi
  ```
  Use the same pattern for prod (e.g. `HindsightProdStack`, prod RDS identifier) if needed.

### pgvector extension not enabled

- **Root cause**: pgvector extension must be enabled on RDS PostgreSQL.
- **Solution**: The CDK code now automatically enables it via a Lambda custom resource. If the Lambda layer for psycopg2 is not available in your region, you may need to:
  1. Create your own Lambda layer with psycopg2-binary, or
  2. Manually enable it:
     ```bash
     # Connect to RDS and run:
     CREATE EXTENSION IF NOT EXISTS vector;
     ```

---

## Security Best Practices

1. **Least privilege**: After testing, replace `AdministratorAccess` with a custom policy that only grants what's needed for CDK/ECS/RDS/etc.

2. **Separate roles**: Dev and prod roles are separate (good for isolation)

3. **Tag restrictions**: The prod role condition uses `refs/tags/*` so only releases can deploy to prod

4. **Secrets rotation**: Rotate `LLM_API_KEY` periodically and update in GitHub Secrets + Secrets Manager

---

## References

- [AWS: Creating OpenID Connect (OIDC) identity providers](https://docs.aws.amazon.com/IAM/latest/UserGuide/id_roles_providers_create_oidc.html)
- [GitHub: Configuring OpenID Connect in Amazon Web Services](https://docs.github.com/en/actions/deployment/security-hardening-your-deployments/configuring-openid-connect-in-amazon-web-services)
- [AWS CDK: Bootstrapping](https://docs.aws.amazon.com/cdk/v2/guide/bootstrapping.html)
