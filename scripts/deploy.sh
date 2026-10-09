#!/usr/bin/env bash
# Build and deploy the RepoPilot Lambda. Idempotent-ish: creates the function if missing,
# otherwise updates the code. Pure stdlib package (boto3 is provided by the Lambda runtime).
set -euo pipefail

REGION="${AWS_REGION:-us-east-1}"
FN="repopilot"
ROLE_NAME="repopilot-lambda-role"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"

cd "$ROOT"
echo "==> Building package"
rm -rf build repopilot-lambda.zip && mkdir build
cp -r repopilot build/
cp lambda_handler.py build/
find build -name "__pycache__" -type d -prune -exec rm -rf {} + 2>/dev/null || true
( cd build && zip -rq ../repopilot-lambda.zip . )
echo "    package: $(du -h repopilot-lambda.zip | cut -f1)"

ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
ROLE_ARN="arn:aws:iam::${ACCOUNT}:role/${ROLE_NAME}"

if ! aws iam get-role --role-name "$ROLE_NAME" >/dev/null 2>&1; then
  echo "==> Creating IAM role $ROLE_NAME"
  aws iam create-role --role-name "$ROLE_NAME" \
    --assume-role-policy-document '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"lambda.amazonaws.com"},"Action":"sts:AssumeRole"}]}'
  aws iam attach-role-policy --role-name "$ROLE_NAME" \
    --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole
  aws iam put-role-policy --role-name "$ROLE_NAME" --policy-name repopilot-bedrock-invoke \
    --policy-document file://iam/bedrock-invoke-policy.json
  echo "    waiting for role propagation..."; sleep 12
fi

if aws lambda get-function --function-name "$FN" --region "$REGION" >/dev/null 2>&1; then
  echo "==> Updating function code"
  aws lambda update-function-code --function-name "$FN" --region "$REGION" \
    --zip-file fileb://repopilot-lambda.zip >/dev/null
else
  echo "==> Creating function"
  aws lambda create-function --function-name "$FN" --region "$REGION" \
    --runtime python3.12 --handler lambda_handler.handler --role "$ROLE_ARN" \
    --timeout 60 --memory-size 512 --zip-file fileb://repopilot-lambda.zip >/dev/null
fi
echo "==> Done. Invoke with:"
echo "    aws lambda invoke --function-name $FN --region $REGION --cli-binary-format raw-in-base64-out --payload '{\"body\":\"{\\\"repo\\\":\\\"pallets/flask\\\"}\"}' out.json && cat out.json"
