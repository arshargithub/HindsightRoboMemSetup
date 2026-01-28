# Hindsight CDK Infrastructure

This directory contains AWS CDK (Python) code to deploy [Hindsight](https://hindsight.vectorize.io) memory service on AWS.

## Architecture

- **VPC** with public/private subnets, NAT Gateway
- **RDS PostgreSQL** with pgvector extension
- **ECS Fargate** running Hindsight API (and optional Control Plane)
- **ALB** for HTTPS access
- **Lambda + EventBridge** for scheduled Reflect jobs
- **Secrets Manager** for RDS credentials, LLM API keys

See `../docs/AWS_HINDSIGHT_ARCHITECTURE.md` for full details.

## Environments

- **dev**: Deploys on push to `main`
- **prod**: Deploys on tag/release creation

## Prerequisites

1. **AWS CLI** configured
2. **Python 3.9+**
3. **Node.js** (for CDK CLI)
4. **CDK CLI**: `npm install -g aws-cdk`

## Setup

1. **Install Python dependencies**:
   ```bash
   cd cdk
   python -m venv .venv
   source .venv/bin/activate  # On Windows: .venv\Scripts\activate
   pip install -r requirements.txt
   ```

2. **Bootstrap CDK** (first time only):
   ```bash
   cdk bootstrap aws://ACCOUNT-ID/ca-central-1
   ```

3. **Configure AWS OIDC** (see `../docs/SETUP_OIDC.md`)

4. **Set GitHub Secrets**:
   - `LLM_API_KEY`: Your OpenAI or Groq API key for Hindsight
   - `RDS_MASTER_PASSWORD`: (Optional) RDS master password. If not set, CDK generates a random one.

5. **Deploy dev**:
   ```bash
   cdk deploy HindsightDevStack
   ```

   Or let GitHub Actions deploy automatically on push to `main`.

## CDK Commands

```bash
# List stacks
cdk list

# Synthesize CloudFormation templates
cdk synth

# Deploy dev
cdk deploy HindsightDevStack

# Deploy prod
cdk deploy HindsightProdStack

# View differences
cdk diff HindsightDevStack

# Destroy (careful!)
cdk destroy HindsightDevStack
```

## Project Structure

```
cdk/
├── app.py              # CDK app entry point
├── hindsight_stack.py  # Main stack (VPC, RDS, ECS, etc.)
├── config.py           # Environment configuration
└── requirements.txt    # Python dependencies
```
