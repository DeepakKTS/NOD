#!/bin/sh
# Nod — remote build and deploy, for a machine with no container runtime.
#
# DEPLOYMENT.md §3's image has never been built: there is no Docker on the
# development machine. This script does the build **on AWS CodeBuild** from a
# source zip in S3, pushes to ECR, and runs the image on App Runner, which
# supplies TLS, a hostname and a health check without an ALB to configure.
#
# GitHub is deliberately not in the path. A CodeBuild GitHub source needs an
# OAuth connection created in the console, and the repository was private when
# this was written (it is public as of Gate 6); a
# source zip needs neither and makes the deploy reproducible from whatever the
# working tree actually contains rather than from what was last pushed.
#
# **Run it with credentials that may create IAM roles.** Everything else here
# is ordinary resource creation; the two roles are the part that needs an
# administrator.
#
# **`make deploy-teardown` does not exist and never did.** It was cited here and in
# DEPLOYMENT.md §3 as though it were real. DEPLOYMENT.md now carries the actual aws
# commands that remove what this script creates. Marked rather than written: an
# untested destructive cloud script is worse than an honest note.
#
# **This whole path is abandoned (ADR-064).** App Runner serves every plain-HTTP route
# and refuses every WebSocket upgrade at its ingress, which is the entire product. Nod
# is deployed on Fly; see `fly.toml`. The script is kept as the record of what was
# tried and why it was dropped, not as a route anyone should take.
#
# Usage:
#     ASSEMBLYAI_API_KEY=... sh scripts/deploy_aws.sh
set -eu

REGION="${AWS_REGION:-us-east-1}"
ACCOUNT="$(aws sts get-caller-identity --query Account --output text)"
REPO="nod"
BUCKET="nod-build-${ACCOUNT}"
IMAGE="${ACCOUNT}.dkr.ecr.${REGION}.amazonaws.com/${REPO}:latest"
: "${ASSEMBLYAI_API_KEY:?set ASSEMBLYAI_API_KEY; INV-5 keeps it server-side}"

echo "==> region ${REGION}, account ${ACCOUNT}"

# --- 1. registry and source bucket -----------------------------------------
aws ecr describe-repositories --repository-names "$REPO" --region "$REGION" \
    >/dev/null 2>&1 ||
    aws ecr create-repository --repository-name "$REPO" --region "$REGION" \
        --image-scanning-configuration scanOnPush=true >/dev/null
aws s3api head-bucket --bucket "$BUCKET" >/dev/null 2>&1 ||
    aws s3 mb "s3://${BUCKET}" --region "$REGION"

# --- 2. roles ---------------------------------------------------------------
# The step that needs an administrator. Split out so a re-run by a
# lower-privileged principal skips it cleanly rather than failing mid-deploy.
if ! aws iam get-role --role-name nod-codebuild >/dev/null 2>&1; then
    aws iam create-role --role-name nod-codebuild --assume-role-policy-document \
        '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"codebuild.amazonaws.com"},"Action":"sts:AssumeRole"}]}' \
        >/dev/null
fi
# Always written, never only on creation: the first run shipped a policy missing
# ecr:UploadLayerPart, and a re-run could not repair it because the role already
# existed. Idempotent put means a fixed policy reaches an existing role.
    aws iam put-role-policy --role-name nod-codebuild --policy-name nod-build \
        --policy-document "$(
            cat <<POLICY
{"Version":"2012-10-17","Statement":[
 {"Effect":"Allow","Action":["logs:CreateLogGroup","logs:CreateLogStream","logs:PutLogEvents"],"Resource":"*"},
 {"Effect":"Allow","Action":["ecr:GetAuthorizationToken"],"Resource":"*"},
 {"Effect":"Allow","Action":["ecr:BatchCheckLayerAvailability","ecr:CompleteLayerUpload","ecr:InitiateLayerUpload","ecr:UploadLayerPart","ecr:PutImage","ecr:BatchGetImage","ecr:GetDownloadUrlForLayer"],"Resource":"arn:aws:ecr:${REGION}:${ACCOUNT}:repository/${REPO}"},
 {"Effect":"Allow","Action":["s3:GetObject","s3:GetObjectVersion"],"Resource":"arn:aws:s3:::${BUCKET}/*"}
]}
POLICY
        )"
if ! aws iam get-role --role-name nod-apprunner-ecr >/dev/null 2>&1; then
    aws iam create-role --role-name nod-apprunner-ecr --assume-role-policy-document \
        '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"build.apprunner.amazonaws.com"},"Action":"sts:AssumeRole"}]}' \
        >/dev/null
    aws iam attach-role-policy --role-name nod-apprunner-ecr --policy-arn \
        arn:aws:iam::aws:policy/service-role/AWSAppRunnerServicePolicyForECRAccess
fi

# --- 3. source zip ----------------------------------------------------------
# `git archive` rather than `zip .`: it ships exactly what is committed, so the
# deployed image cannot contain an uncommitted edit or, worse, `.env`.
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
git archive --format=zip -o "$TMP/src.zip" HEAD
cat > "$TMP/buildspec.yml" <<'SPEC'
version: 0.2
phases:
  pre_build:
    commands:
      - aws ecr get-login-password --region "$AWS_REGION" | docker login --username AWS --password-stdin "$ECR"
      # DEPLOYMENT.md §3 requires a digest pin and the Dockerfile carries a tag.
      # Resolve it here, where a container runtime exists, and record it — this
      # is the machine the TODO said to do it on.
      - docker pull python:3.12-slim
      - docker image inspect python:3.12-slim --format '{{index .RepoDigests 0}}'
  build:
    commands:
      - docker build -t "$ECR:latest" .
      - docker images "$ECR:latest" --format 'image size {{.Size}}'
  post_build:
    commands:
      - docker push "$ECR:latest"
SPEC
(cd "$TMP" && zip -q src.zip buildspec.yml)
aws s3 cp "$TMP/src.zip" "s3://${BUCKET}/src.zip" --region "$REGION" >/dev/null

# --- 4. build ---------------------------------------------------------------
aws codebuild delete-project --name nod-build --region "$REGION" >/dev/null 2>&1 || true
aws codebuild create-project --region "$REGION" --name nod-build \
    --source "type=S3,location=${BUCKET}/src.zip,buildspec=buildspec.yml" \
    --artifacts type=NO_ARTIFACTS \
    --environment "type=LINUX_CONTAINER,image=aws/codebuild/standard:7.0,computeType=BUILD_GENERAL1_SMALL,privilegedMode=true,environmentVariables=[{name=ECR,value=${ACCOUNT}.dkr.ecr.${REGION}.amazonaws.com/${REPO}}]" \
    --service-role "arn:aws:iam::${ACCOUNT}:role/nod-codebuild" >/dev/null
BUILD_ID="$(aws codebuild start-build --project-name nod-build --region "$REGION" \
    --query 'build.id' --output text)"
echo "==> building ${BUILD_ID}"
while :; do
    STATUS="$(aws codebuild batch-get-builds --ids "$BUILD_ID" --region "$REGION" \
        --query 'builds[0].buildStatus' --output text)"
    [ "$STATUS" = "IN_PROGRESS" ] || break
    sleep 15
done
echo "==> build ${STATUS}"
[ "$STATUS" = "SUCCEEDED" ] || {
    echo "build failed; logs:"
    aws codebuild batch-get-builds --ids "$BUILD_ID" --region "$REGION" \
        --query 'builds[0].logs.deepLink' --output text
    exit 1
}

# --- 5. service -------------------------------------------------------------
# NOD_MAX_SESSIONS=2 (ADR-042): the account permits 5 concurrent upstream
# streams and a rotating session holds 2, so a demo holding 2 leaves 3.
# **A live sweep and this URL share the account's 16 s start-rate gate and must
# never overlap** — one 1008 anywhere voids a sweep (ADR-048, DEPLOYMENT §1).
ENV_VARS="ASSEMBLYAI_API_KEY=${ASSEMBLYAI_API_KEY},NOD_ENV=prod,NOD_MAX_SESSIONS=2,NOD_MODE_DEFAULT=observe,NOD_TRACE_DIR=/data/traces,NOD_DB_PATH=/data/nod.db"
# App Runner requires a service name of at least 4 characters; "nod" is 3.
SERVICE="nod-demo"
aws apprunner create-service --region "$REGION" --service-name "$SERVICE" \
    --source-configuration "$(
        cat <<CFG
{"ImageRepository":{"ImageIdentifier":"${IMAGE}","ImageRepositoryType":"ECR",
 "ImageConfiguration":{"Port":"8000","RuntimeEnvironmentVariables":{$(
            echo "$ENV_VARS" | awk -F, '{for(i=1;i<=NF;i++){split($i,a,"=");printf "%s\"%s\":\"%s\"",(i>1?",":""),a[1],substr($i,length(a[1])+2)}}'
        )}}},
 "AutoDeploymentsEnabled":false,
 "AuthenticationConfiguration":{"AccessRoleArn":"arn:aws:iam::${ACCOUNT}:role/nod-apprunner-ecr"}}
CFG
    )" \
    --health-check-configuration 'Protocol=HTTP,Path=/healthz,Interval=10,Timeout=5,HealthyThreshold=1,UnhealthyThreshold=5' \
    --instance-configuration 'Cpu=1024,Memory=2048' >/dev/null

echo "==> waiting for the service"
while :; do
    ARN="$(aws apprunner list-services --region "$REGION" \
        --query "ServiceSummaryList[?ServiceName=='${SERVICE}'].ServiceArn" --output text)"
    STATE="$(aws apprunner describe-service --service-arn "$ARN" --region "$REGION" \
        --query 'Service.Status' --output text)"
    [ "$STATE" = "OPERATION_IN_PROGRESS" ] || break
    sleep 20
done
URL="https://$(aws apprunner describe-service --service-arn "$ARN" --region "$REGION" \
    --query 'Service.ServiceUrl' --output text)"
echo "==> ${STATE}  ${URL}"
echo "==> /healthz $(curl -s -o /dev/null -w '%{http_code}' "${URL}/healthz")"
echo "==> /readyz  $(curl -s -o /dev/null -w '%{http_code}' "${URL}/readyz")"
echo "$URL"
