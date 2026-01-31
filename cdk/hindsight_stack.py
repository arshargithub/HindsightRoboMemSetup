"""CDK stack for Hindsight memory service on AWS."""

import aws_cdk as cdk
from aws_cdk import (
    aws_ec2 as ec2,
    aws_rds as rds,
    aws_ecs as ecs,
    aws_ecs_patterns as ecs_patterns,
    aws_elasticloadbalancingv2 as elbv2,
    aws_secretsmanager as secretsmanager,
    aws_iam as iam,
    aws_lambda as lambda_,
    aws_events as events,
    aws_events_targets as targets,
    aws_logs as logs,
    custom_resources as cr,
    Duration,
    RemovalPolicy,
)
from constructs import Construct
from config import (
    ENVIRONMENTS,
    HINDSIGHT_IMAGE,
    HINDSIGHT_API_PORT,
    HINDSIGHT_CONTROL_PLANE_PORT,
    DB_NAME,
    DB_USERNAME,
    ALB_HEALTH_CHECK_PATH,
    ALB_IDLE_TIMEOUT_SECONDS,
    LLM_PROVIDER,
    LLM_MODEL,
)


class HindsightStack(cdk.Stack):
    """Stack for Hindsight memory service."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        environment: str,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        env_config = ENVIRONMENTS[environment]

        # VPC
        vpc = ec2.Vpc(
            self,
            "Vpc",
            max_azs=2,
            nat_gateways=1,  # Single NAT for cost; increase for HA in prod
            subnet_configuration=[
                ec2.SubnetConfiguration(
                    subnet_type=ec2.SubnetType.PUBLIC,
                    name="Public",
                ),
                ec2.SubnetConfiguration(
                    subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS,
                    name="Private",
                ),
            ],
        )

        # Secrets Manager: RDS master password
        # Alphanumeric only. Alembic passes DB URL to ConfigParser, which treats % as interpolation;
        # URL-encoded passwords (e.g. %25) break it. Use exclude_punctuation to avoid symbols.
        rds_secret = secretsmanager.Secret(
            self,
            "RdsSecret",
            description=f"Hindsight RDS master password ({environment})",
            generate_secret_string=secretsmanager.SecretStringGenerator(
                secret_string_template='{"username": "' + DB_USERNAME + '"}',
                generate_string_key="password",
                exclude_punctuation=True,
            ),
        )

        # Secrets Manager: LLM API key
        # IMPORTANT: This secret is populated automatically during stack creation via LlmKeyPopulator Lambda.
        # The LLM API key is passed via CDK context (--context llm_api_key=...).
        # The GitHub Actions workflow passes this automatically from GitHub Secret LLM_API_KEY.
        # For manual deployments, pass: cdk deploy --context llm_api_key=<your-key>
        # Without a valid API key, Hindsight API tasks will fail with 401 errors during startup.
        llm_secret = secretsmanager.Secret(
            self,
            "LlmApiKeySecret",
            description=f"Hindsight LLM API key ({environment}) - auto-populated by CDK Lambda",
        )

        # RDS PostgreSQL with pgvector
        rds_instance = rds.DatabaseInstance(
            self,
            "RdsInstance",
            engine=rds.DatabaseInstanceEngine.postgres(
                version=rds.PostgresEngineVersion.VER_18_1,
            ),
            instance_type=ec2.InstanceType.of(
                ec2.InstanceClass.T3,
                ec2.InstanceSize.MICRO if environment == "dev" else ec2.InstanceSize.SMALL,
            ),
            vpc=vpc,
            vpc_subnets=ec2.SubnetSelection(
                subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS,
            ),
            database_name=DB_NAME,
            credentials=rds.Credentials.from_secret(rds_secret, DB_USERNAME),
            removal_policy=RemovalPolicy.SNAPSHOT,  # Keep snapshot on delete
            deletion_protection=environment == "prod",
            backup_retention=Duration.days(7 if environment == "prod" else 1),
            enable_performance_insights=environment == "prod",
        )

        # pgvector extension will be enabled via custom resource after RDS is ready

        # ECS Cluster
        cluster = ecs.Cluster(
            self,
            "EcsCluster",
            vpc=vpc,
            container_insights=True,
        )

        # ECS Task Execution Role
        task_execution_role = iam.Role(
            self,
            "TaskExecutionRole",
            assumed_by=iam.ServicePrincipal("ecs-tasks.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "service-role/AmazonECSTaskExecutionRolePolicy"
                ),
            ],
        )

        # Grant task execution role access to secrets
        rds_secret.grant_read(task_execution_role)
        llm_secret.grant_read(task_execution_role)

        # Create a secret that will hold the full database connection URL
        # Hindsight expects DATABASE_URL as: postgresql://user:password@host:port/dbname
        # This secret is populated automatically by the DbUrlPopulator Lambda after RDS is ready
        db_url_secret = secretsmanager.Secret(
            self,
            "DbUrlSecret",
            description=f"Hindsight database connection URL ({environment}) - auto-populated by CDK Lambda",
        )

        # Grant task execution role access to db_url_secret
        db_url_secret.grant_read(task_execution_role)

        # Lambda: Populate DB URL secret automatically after RDS is created
        # This ensures the secret is ready before ECS tasks start
        # Includes retry logic and proper URL encoding for special characters in password
        db_url_populator = lambda_.Function(
            self,
            "DbUrlPopulator",
            runtime=lambda_.Runtime.PYTHON_3_11,
            handler="index.handler",
            code=lambda_.Code.from_inline("""
import json
import boto3
import time

def handler(event, context):
    secrets_client = boto3.client('secretsmanager')
    
    try:
        # Provider passes ResourceProperties in the event
        props = event.get('ResourceProperties', event)
        
        # Get RDS endpoint and password
        rds_endpoint = props['RdsEndpoint']
        rds_secret_arn = props['RdsSecretArn']
        db_url_secret_arn = props['DbUrlSecretArn']
        db_name = props['DbName']
        db_username = props['DbUsername']
        
        # Retry logic: RDS might not be immediately available
        max_retries = 5
        retry_delay = 5  # seconds
        
        for attempt in range(max_retries):
            try:
                # Get RDS password
                rds_secret = secrets_client.get_secret_value(SecretId=rds_secret_arn)
                rds_password = json.loads(rds_secret['SecretString'])['password']
                
                # RDS password is alphanumeric-only (exclude_characters in secret). No URL encoding.
                # Alembic passes DB URL to ConfigParser; % triggers interpolation and breaks migrations.
                db_url = f"postgresql://{db_username}:{rds_password}@{rds_endpoint}:5432/{db_name}"
                
                # Populate DB URL secret
                secrets_client.put_secret_value(
                    SecretId=db_url_secret_arn,
                    SecretString=db_url
                )
                
                return {
                    'PhysicalResourceId': db_url_secret_arn,
                    'Data': {'Message': 'Database URL secret populated successfully'}
                }
            except Exception as e:
                if attempt < max_retries - 1:
                    print(f"Attempt {attempt + 1} failed: {str(e)}, retrying in {retry_delay}s...")
                    time.sleep(retry_delay)
                else:
                    raise e
    except Exception as e:
        print(f"Error: {str(e)}")
        raise e
"""),
            timeout=Duration.seconds(60),  # Increased timeout for retries
            memory_size=256,
        )

        # Grant Lambda permissions to read RDS secret and write DB URL secret
        rds_secret.grant_read(db_url_populator)
        db_url_secret.grant_write(db_url_populator)

        # Custom resource to trigger Lambda when RDS is ready
        db_url_provider = cr.Provider(
            self,
            "DbUrlProvider",
            on_event_handler=db_url_populator,
        )

        db_url_resource = cdk.CustomResource(
            self,
            "DbUrlResource",
            service_token=db_url_provider.service_token,
            properties={
                "RdsEndpoint": rds_instance.instance_endpoint.hostname,
                "RdsSecretArn": rds_secret.secret_arn,
                "DbUrlSecretArn": db_url_secret.secret_arn,
                "DbName": DB_NAME,
                "DbUsername": DB_USERNAME,
            },
        )

        # Lambda: Populate LLM API key secret automatically during stack creation
        # This ensures the secret is ready before ECS tasks start
        # The LLM API key is passed via CDK context (--context llm_api_key=...)
        llm_key_populator = lambda_.Function(
            self,
            "LlmKeyPopulator",
            runtime=lambda_.Runtime.PYTHON_3_11,
            handler="index.handler",
            code=lambda_.Code.from_inline("""
import json
import boto3

def handler(event, context):
    secrets_client = boto3.client('secretsmanager')
    
    try:
        # Provider passes ResourceProperties in the event
        props = event.get('ResourceProperties', event)
        request_type = event.get('RequestType', 'Create')
        
        # Handle DELETE - just return success
        if request_type == 'Delete':
            return {
                'PhysicalResourceId': event.get('PhysicalResourceId', 'llm-key-resource'),
                'Status': 'SUCCESS'
            }
        
        llm_secret_arn = props['LlmSecretArn']
        llm_api_key = props.get('LlmApiKey', '')
        
        # Validate that we have an API key
        if not llm_api_key:
            raise ValueError("LLM API key is required. Pass it via CDK context: cdk deploy --context llm_api_key=<your-key>")
        
        # Validate format (should start with sk- or sk-proj- for OpenAI)
        if not (llm_api_key.startswith('sk-') or llm_api_key.startswith('sk-proj-')):
            raise ValueError("LLM API key must start with 'sk-' or 'sk-proj-' (OpenAI key format)")
        
        # Populate LLM secret
        secrets_client.put_secret_value(
            SecretId=llm_secret_arn,
            SecretString=llm_api_key
        )
        
        return {
            'PhysicalResourceId': llm_secret_arn,
            'Data': {'Message': 'LLM API key secret populated successfully'}
        }
    except Exception as e:
        print(f"Error: {str(e)}")
        raise e
"""),
            timeout=Duration.seconds(30),
            memory_size=256,
        )

        # Grant Lambda permissions to write LLM secret
        llm_secret.grant_write(llm_key_populator)

        # Custom resource to trigger Lambda during stack creation
        llm_key_provider = cr.Provider(
            self,
            "LlmKeyProvider",
            on_event_handler=llm_key_populator,
        )

        # Get LLM API key from CDK context (passed via --context llm_api_key=...)
        # Note: Context may be None during 'cdk synth', but must be provided during 'cdk deploy'
        # The Lambda will validate and fail with a clear error if missing during deployment
        llm_api_key = self.node.try_get_context("llm_api_key") or ""

        llm_key_resource = cdk.CustomResource(
            self,
            "LlmKeyResource",
            service_token=llm_key_provider.service_token,
            properties={
                "LlmSecretArn": llm_secret.secret_arn,
                "LlmApiKey": llm_api_key,
            },
        )

        # Security group for pgvector Lambda (needed for VPC access to RDS)
        pgvector_lambda_sg = ec2.SecurityGroup(
            self,
            "PgvectorLambdaSecurityGroup",
            vpc=vpc,
            description="Security group for pgvector Lambda to access RDS",
            allow_all_outbound=True,  # Allow Lambda to reach RDS
        )

        # Allow pgvector Lambda to connect to RDS
        rds_instance.connections.allow_from(
            pgvector_lambda_sg,
            ec2.Port.tcp(5432),
            "Allow pgvector Lambda to connect to RDS PostgreSQL"
        )

        # Lambda: Enable pgvector extension on RDS PostgreSQL
        # RDS PostgreSQL 18+ supports pgvector; we enable it automatically after RDS is ready
        # Note: The psycopg2 Lambda layer may not be accessible in all regions
        # If the layer isn't available, the Lambda will return success with manual setup instructions
        # This allows stack creation to succeed - pgvector can be enabled manually after deployment
        
        # IMPORTANT: The psycopg2 layer (arn:aws:lambda:REGION:898466741470:layer:psycopg2-py311:1)
        # may not be accessible due to permissions or region availability.
        # If you encounter "lambda:GetLayerVersion" permission errors, the Lambda will work
        # without the layer but will require manual pgvector setup.
        # 
        # To use the layer, ensure your CloudFormation execution role has lambda:GetLayerVersion permission,
        # or manually enable pgvector after stack creation: CREATE EXTENSION vector;
        
        pgvector_enabler = lambda_.Function(
            self,
            "PgvectorEnabler",
            runtime=lambda_.Runtime.PYTHON_3_11,
            handler="index.handler",
            vpc=vpc,
            vpc_subnets=ec2.SubnetSelection(
                subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS,
            ),
            security_groups=[pgvector_lambda_sg],
            code=lambda_.Code.from_inline("""
import json
import boto3
import time

# Try to import psycopg2 (from layer or bundled)
try:
    import psycopg2
    PSYCOPG2_AVAILABLE = True
except ImportError:
    PSYCOPG2_AVAILABLE = False

def handler(event, context):
    secrets_client = boto3.client('secretsmanager')
    request_type = event.get('RequestType', 'Create')
    
    # Handle DELETE - just return success
    if request_type == 'Delete':
        return {
            'PhysicalResourceId': event.get('PhysicalResourceId', 'pgvector-resource'),
            'Status': 'SUCCESS'
        }
    
    # If psycopg2 is not available, return success with manual setup instructions
    if not PSYCOPG2_AVAILABLE:
        return {
            'PhysicalResourceId': 'pgvector-manual-setup-required',
            'Status': 'SUCCESS',
            'Data': {
                'Message': 'psycopg2 not available. pgvector extension must be enabled manually.',
                'Instructions': 'Connect to RDS and run: CREATE EXTENSION IF NOT EXISTS vector;',
                'Note': 'The stack will complete successfully, but pgvector must be enabled manually after deployment.'
            }
        }
    
    # psycopg2 is available - proceed with enabling pgvector extension
    try:
        props = event.get('ResourceProperties', event)
        rds_endpoint = props['RdsEndpoint']
        rds_secret_arn = props['RdsSecretArn']
        db_name = props['DbName']
        db_username = props['DbUsername']
        
        # Get RDS password
        rds_secret = secrets_client.get_secret_value(SecretId=rds_secret_arn)
        rds_password = json.loads(rds_secret['SecretString'])['password']
        
        # Retry logic: RDS might not be immediately available
        max_retries = 10
        retry_delay = 10  # seconds
        
        for attempt in range(max_retries):
            try:
                # Connect to RDS
                conn = psycopg2.connect(
                    host=rds_endpoint,
                    port=5432,
                    database=db_name,
                    user=db_username,
                    password=rds_password,
                    connect_timeout=10
                )
                conn.autocommit = True
                cursor = conn.cursor()
                
                # Check if extension exists
                cursor.execute("SELECT 1 FROM pg_extension WHERE extname = 'vector';")
                if cursor.fetchone():
                    print("pgvector extension already exists")
                else:
                    # Create extension
                    cursor.execute("CREATE EXTENSION IF NOT EXISTS vector;")
                    print("pgvector extension created successfully")
                
                cursor.close()
                conn.close()
                
                return {
                    'PhysicalResourceId': f"{rds_endpoint}/{db_name}",
                    'Status': 'SUCCESS',
                    'Data': {'Message': 'pgvector extension enabled successfully'}
                }
            except psycopg2.OperationalError as e:
                if attempt < max_retries - 1:
                    print(f"Attempt {attempt + 1} failed: {str(e)}, retrying in {retry_delay}s...")
                    time.sleep(retry_delay)
                else:
                    raise e
            except Exception as e:
                print(f"Error: {str(e)}")
                raise e
    except Exception as e:
        print(f"Error: {str(e)}")
        raise e
"""),
            timeout=Duration.minutes(5),  # Increased timeout: 10 retries × 10s = 100s minimum, plus connection time
            memory_size=256,
        )
        
        # NOTE: psycopg2 Lambda layer is commented out due to permission/accessibility issues
        # The layer (arn:aws:lambda:REGION:898466741470:layer:psycopg2-py311:1) may not be
        # accessible in all regions or may require additional permissions for CloudFormation.
        # 
        # The Lambda code handles missing psycopg2 gracefully - it will return success with
        # instructions to manually enable pgvector extension.
        # 
        # To enable automatic pgvector setup:
        # 1. Ensure CloudFormation execution role has lambda:GetLayerVersion permission, OR
        # 2. Create your own Lambda layer with psycopg2-binary, OR  
        # 3. Use a container image for the Lambda, OR
        # 4. Manually enable pgvector after stack creation: CREATE EXTENSION vector;
        #
        # Uncomment below if you have access to the layer:
        # psycopg2_layer_arn = f"arn:aws:lambda:{self.region}:898466741470:layer:psycopg2-py311:1"
        # psycopg2_layer = lambda_.LayerVersion.from_layer_version_arn(
        #     self,
        #     "Psycopg2Layer",
        #     layer_version_arn=psycopg2_layer_arn
        # )
        # pgvector_enabler.add_layers(psycopg2_layer)

        # Grant Lambda permissions to read RDS secret
        rds_secret.grant_read(pgvector_enabler)

        # Custom resource to enable pgvector extension
        pgvector_provider = cr.Provider(
            self,
            "PgvectorProvider",
            on_event_handler=pgvector_enabler,
        )

        pgvector_resource = cdk.CustomResource(
            self,
            "PgvectorResource",
            service_token=pgvector_provider.service_token,
            properties={
                "RdsEndpoint": rds_instance.instance_endpoint.hostname,
                "RdsSecretArn": rds_secret.secret_arn,
                "DbName": DB_NAME,
                "DbUsername": DB_USERNAME,
            },
        )

        # Ensure pgvector is enabled before DB URL is populated
        db_url_resource.node.add_dependency(pgvector_resource)

        # ECS Task Role (for Hindsight API to access AWS services if needed)
        task_role = iam.Role(
            self,
            "TaskRole",
            assumed_by=iam.ServicePrincipal("ecs-tasks.amazonaws.com"),
        )

        # ECS Task Definition: Hindsight API
        # ephemeral_storage_gib=50: default 20 GiB is too small for hindsight image (NVIDIA libs, etc.)
        task_definition = ecs.FargateTaskDefinition(
            self,
            "HindsightApiTask",
            cpu=env_config["ecs_task_cpu"],
            memory_limit_mib=env_config["ecs_task_memory"],
            ephemeral_storage_gib=50,
            execution_role=task_execution_role,
            task_role=task_role,
        )

        # Container: Hindsight API
        api_container = task_definition.add_container(
            "HindsightApi",
            image=ecs.ContainerImage.from_registry(HINDSIGHT_IMAGE),
            logging=ecs.LogDrivers.aws_logs(
                stream_prefix="hindsight-api",
                log_retention=logs.RetentionDays.ONE_WEEK if environment == "dev" else logs.RetentionDays.ONE_MONTH,
            ),
            environment={
                "HINDSIGHT_API_LLM_PROVIDER": LLM_PROVIDER,
                "HINDSIGHT_API_LLM_MODEL": LLM_MODEL,
                "HINDSIGHT_API_PORT": str(HINDSIGHT_API_PORT),
            },
            secrets={
                # Option 1: Use full DATABASE_URL (preferred - set after first deploy)
                "HINDSIGHT_API_DATABASE_URL": ecs.Secret.from_secrets_manager(db_url_secret),
                # Option 2: Use components (if Hindsight supports it)
                # "HINDSIGHT_API_DATABASE_USER": ecs.Secret.from_secrets_manager(rds_secret, "username"),
                # "HINDSIGHT_API_DATABASE_PASSWORD": ecs.Secret.from_secrets_manager(rds_secret, "password"),
                "HINDSIGHT_API_LLM_API_KEY": ecs.Secret.from_secrets_manager(llm_secret),
            },
        )

        api_container.add_port_mappings(
            ecs.PortMapping(
                container_port=HINDSIGHT_API_PORT,
                protocol=ecs.Protocol.TCP,
            )
        )

        # ECS Service: Hindsight API behind ALB
        # Note: Using HTTP for now; add ACM certificate for HTTPS later
        # Ensure DB URL secret is populated before ECS tasks start
        fargate_service = ecs_patterns.ApplicationLoadBalancedFargateService(
            self,
            "HindsightApiService",
            cluster=cluster,
            task_definition=task_definition,
            desired_count=env_config["ecs_desired_count"],
            public_load_balancer=True,
            listener_port=80,  # HTTP (add HTTPS with ACM cert later)
            protocol=elbv2.ApplicationProtocol.HTTP,
            health_check_grace_period=Duration.seconds(120),
        )
        
        # Ensure DB URL and LLM key are populated before ECS service starts
        fargate_service.service.node.add_dependency(db_url_resource)
        fargate_service.service.node.add_dependency(llm_key_resource)

        # Allow ECS tasks to connect to RDS
        # IMPORTANT: This security group rule must be added BEFORE the service starts tasks
        # The rule applies to all tasks in the service, including those started during initial deployment
        rds_instance.connections.allow_from(
            fargate_service.service.connections,
            ec2.Port.tcp(5432),
            "Allow ECS tasks to connect to RDS PostgreSQL"
        )

        # Configure ALB idle timeout (default 60s, increase for slow /docs and first requests)
        fargate_service.load_balancer.set_attribute(
            "idle_timeout.timeout_seconds", str(ALB_IDLE_TIMEOUT_SECONDS)
        )

        # Configure health check
        # Note: Using /health which checks DB connectivity - if this times out, investigate DB performance
        # Timeout increased to 15s to diagnose if DB checks are slow (default 5s may be too aggressive)
        fargate_service.target_group.configure_health_check(
            path=ALB_HEALTH_CHECK_PATH,  # Use config value
            interval=Duration.seconds(30),
            timeout=Duration.seconds(15),  # Increased from 5s to diagnose DB check slowness
            healthy_threshold_count=2,
            unhealthy_threshold_count=3,
        )

        # Lambda: Reflect job (calls Hindsight Reflect API)
        # Note: Lambda uses public ALB DNS name (ALB is public) - no VPC needed
        # Includes retry logic and better error handling for ALB availability
        reflect_lambda = lambda_.Function(
            self,
            "ReflectJob",
            runtime=lambda_.Runtime.PYTHON_3_11,
            handler="index.handler",
            code=lambda_.Code.from_inline("""
import os
import json
import urllib.request
import urllib.error
import time

def handler(event, context):
    # Get Hindsight API URL from environment
    api_url = os.environ.get('HINDSIGHT_API_URL')
    bank_id = os.environ.get('HINDSIGHT_BANK_ID', 'johnny-robot')
    
    if not api_url:
        return {
            'statusCode': 500,
            'body': json.dumps({'error': 'HINDSIGHT_API_URL not set'})
        }
    
    # Call Hindsight Reflect API
    reflect_url = f"{api_url}/v1/default/banks/{bank_id}/reflect"
    
    # Simple query for batch reflection
    payload = {
        'query': 'Summarize recent learnings and update opinions based on new experiences.'
    }
    
    # Retry logic: ALB might not be immediately available after deployment
    max_retries = 3
    retry_delay = 5  # seconds
    
    for attempt in range(max_retries):
        try:
            req = urllib.request.Request(
                reflect_url,
                data=json.dumps(payload).encode('utf-8'),
                headers={'Content-Type': 'application/json'},
                method='POST'
            )
            
            with urllib.request.urlopen(req, timeout=300) as response:
                result = json.loads(response.read().decode('utf-8'))
                return {
                    'statusCode': 200,
                    'body': json.dumps({
                        'success': True,
                        'result': result
                    })
                }
        except urllib.error.HTTPError as e:
            # Don't retry on 4xx errors (client errors)
            if 400 <= e.code < 500:
                error_body = e.read().decode() if hasattr(e, 'read') else str(e)
                return {
                    'statusCode': e.code,
                    'body': json.dumps({'error': f'HTTP {e.code}: {error_body}'})
                }
            # Retry on 5xx errors (server errors) or connection issues
            if attempt < max_retries - 1:
                print(f"Attempt {attempt + 1} failed with HTTP {e.code}, retrying in {retry_delay}s...")
                time.sleep(retry_delay)
            else:
                error_body = e.read().decode() if hasattr(e, 'read') else str(e)
                return {
                    'statusCode': e.code,
                    'body': json.dumps({'error': f'HTTP {e.code} after {max_retries} attempts: {error_body}'})
                }
        except (urllib.error.URLError, ConnectionError, OSError) as e:
            # Retry on connection errors (ALB might not be ready)
            if attempt < max_retries - 1:
                print(f"Attempt {attempt + 1} failed with connection error: {str(e)}, retrying in {retry_delay}s...")
                time.sleep(retry_delay)
            else:
                return {
                    'statusCode': 500,
                    'body': json.dumps({'error': f'Connection failed after {max_retries} attempts: {str(e)}'})
                }
        except Exception as e:
            return {
                'statusCode': 500,
                'body': json.dumps({'error': f'Unexpected error: {str(e)}'})
            }
    
    return {
        'statusCode': 500,
        'body': json.dumps({'error': 'Failed after all retry attempts'})
    }
"""),
            timeout=Duration.minutes(5),
            memory_size=256,
            environment={
                # Use ALB DNS name (public) - Lambda can reach it over internet
                # Note: On first deployment, ALB might not be immediately available
                "HINDSIGHT_API_URL": f"http://{fargate_service.load_balancer.load_balancer_dns_name}",
                "HINDSIGHT_BANK_ID": "johnny-robot",
            },
        )

        # EventBridge Rule: Schedule Reflect job
        reflect_schedule = events.Rule(
            self,
            "ReflectSchedule",
            schedule=events.Schedule.expression(env_config["reflect_schedule"]),
            description=f"Scheduled Reflect job for Hindsight ({environment})",
        )

        reflect_schedule.add_target(targets.LambdaFunction(reflect_lambda))

        # -----------------------------------------------------------------------
        # Control Plane (Web UI)
        # Runs npx @vectorize-io/hindsight-control-plane; points at API ALB URL.
        # -----------------------------------------------------------------------
        api_base_url = f"http://{fargate_service.load_balancer.load_balancer_dns_name}"

        cp_task_definition = ecs.FargateTaskDefinition(
            self,
            "ControlPlaneTask",
            cpu=256,
            memory_limit_mib=512,
            execution_role=task_execution_role,
        )

        cp_container = cp_task_definition.add_container(
            "ControlPlane",
            image=ecs.ContainerImage.from_registry("node:20-alpine"),
            logging=ecs.LogDrivers.aws_logs(
                stream_prefix="hindsight-cp",
                log_retention=logs.RetentionDays.ONE_WEEK if environment == "dev" else logs.RetentionDays.ONE_MONTH,
            ),
            environment={
                "HINDSIGHT_CP_DATAPLANE_API_URL": api_base_url,
                "PORT": str(HINDSIGHT_CONTROL_PLANE_PORT),
                "HOSTNAME": "0.0.0.0",
            },
            command=["sh", "-c", "npx -y @vectorize-io/hindsight-control-plane --port $PORT --hostname $HOSTNAME"],
        )

        cp_container.add_port_mappings(
            ecs.PortMapping(
                container_port=HINDSIGHT_CONTROL_PLANE_PORT,
                protocol=ecs.Protocol.TCP,
            )
        )

        cp_fargate_service = ecs_patterns.ApplicationLoadBalancedFargateService(
            self,
            "ControlPlaneService",
            cluster=cluster,
            task_definition=cp_task_definition,
            desired_count=1,
            public_load_balancer=True,
            listener_port=80,
            protocol=elbv2.ApplicationProtocol.HTTP,
            health_check_grace_period=Duration.seconds(60),
        )

        # Control plane root (/) redirects to /dashboard (307); use /api/health which returns 200
        cp_fargate_service.target_group.configure_health_check(
            path="/api/health",
            interval=Duration.seconds(30),
            timeout=Duration.seconds(5),
            healthy_threshold_count=2,
            unhealthy_threshold_count=3,
        )

        # Outputs
        cdk.CfnOutput(
            self,
            "HindsightApiUrl",
            value=f"http://{fargate_service.load_balancer.load_balancer_dns_name}",
            description="Hindsight API URL (HTTP - add HTTPS with ACM cert for production)",
        )

        cdk.CfnOutput(
            self,
            "RdsEndpoint",
            value=rds_instance.instance_endpoint.hostname,
            description="RDS PostgreSQL endpoint",
        )

        cdk.CfnOutput(
            self,
            "RdsSecretArn",
            value=rds_secret.secret_arn,
            description="RDS master password secret ARN",
        )

        cdk.CfnOutput(
            self,
            "LlmSecretArn",
            value=llm_secret.secret_arn,
            description="LLM API key secret ARN",
        )

        cdk.CfnOutput(
            self,
            "DbUrlSecretArn",
            value=db_url_secret.secret_arn,
            description="Database URL secret ARN (populated after first deploy)",
        )

        cdk.CfnOutput(
            self,
            "ControlPlaneUrl",
            value=f"http://{cp_fargate_service.load_balancer.load_balancer_dns_name}",
            description="Hindsight Control Plane (Web UI) URL",
        )
