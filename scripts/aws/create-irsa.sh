#!/usr/bin/env bash
# Create/update the IRSA role used by GOPS pods for S3 and Secrets Manager.
set -euo pipefail

AWS_REGION="${AWS_REGION:-ap-northeast-2}"
CLUSTER_NAME="${CLUSTER_NAME:-gops-eks-cluster}"
NAMESPACE="${NAMESPACE:-alfaka-market-data}"
SERVICE_ACCOUNT="${SERVICE_ACCOUNT:-alfaka-market-data-sa}"
ROLE_NAME="${ROLE_NAME:-alfaka-dev-market-data-irsa}"
POLICY_NAME="${POLICY_NAME:-alfaka-dev-market-data-pod-policy}"
S3_BUCKET="${S3_BUCKET:-gops-market-data-<aws-account-id>-ap-northeast-2-an}"
ALPACA_SECRET_NAME="${ALPACA_SECRET_NAME:-dev/alpaca}"
KIS_SECRET_NAME="${KIS_SECRET_NAME:-tead/gops/kis}"
GOOGLE_OAUTH_SECRET_NAME="${GOOGLE_OAUTH_SECRET_NAME:-}"

ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
ISSUER_URL="$(aws eks describe-cluster --region "${AWS_REGION}" --name "${CLUSTER_NAME}" --query 'cluster.identity.oidc.issuer' --output text)"
OIDC_PROVIDER_URL="${ISSUER_URL#https://}"
OIDC_PROVIDER_ARN="arn:aws:iam::${ACCOUNT_ID}:oidc-provider/${OIDC_PROVIDER_URL}"
ROLE_ARN="arn:aws:iam::${ACCOUNT_ID}:role/${ROLE_NAME}"
POLICY_ARN="arn:aws:iam::${ACCOUNT_ID}:policy/${POLICY_NAME}"
S3_BUCKET_ARN="arn:aws:s3:::${S3_BUCKET}"
ALPACA_SECRET_ARN="$(aws secretsmanager describe-secret --region "${AWS_REGION}" --secret-id "${ALPACA_SECRET_NAME}" --query ARN --output text)"
KIS_SECRET_ARN="$(aws secretsmanager describe-secret --region "${AWS_REGION}" --secret-id "${KIS_SECRET_NAME}" --query ARN --output text)"
SECRET_RESOURCE_LINES="\"${ALPACA_SECRET_ARN}\",
        \"${KIS_SECRET_ARN}\""
if [[ -n "${GOOGLE_OAUTH_SECRET_NAME}" ]]; then
  GOOGLE_OAUTH_SECRET_ARN="$(aws secretsmanager describe-secret --region "${AWS_REGION}" --secret-id "${GOOGLE_OAUTH_SECRET_NAME}" --query ARN --output text)"
  SECRET_RESOURCE_LINES="${SECRET_RESOURCE_LINES},
        \"${GOOGLE_OAUTH_SECRET_ARN}\""
fi

ensure_oidc_provider() {
  if aws iam get-open-id-connect-provider --open-id-connect-provider-arn "${OIDC_PROVIDER_ARN}" >/dev/null 2>&1; then
    echo "exists: ${OIDC_PROVIDER_ARN}"
    return
  fi

  issuer_host="${OIDC_PROVIDER_URL%%/*}"
  thumbprint="$(
    openssl s_client -servername "${issuer_host}" -showcerts -connect "${issuer_host}:443" </dev/null 2>/dev/null |
      sed -n '/BEGIN CERTIFICATE/,/END CERTIFICATE/p' |
      awk 'BEGIN { n = 0 } /BEGIN CERTIFICATE/ { n += 1; cert[n] = $0 ORS; next } n > 0 { cert[n] = cert[n] $0 ORS } END { printf "%s", cert[n] }' |
      openssl x509 -fingerprint -noout -sha1 |
      sed 's/.*=//; s/://g' |
      tr '[:upper:]' '[:lower:]'
  )"

  aws iam create-open-id-connect-provider \
    --url "${ISSUER_URL}" \
    --client-id-list sts.amazonaws.com \
    --thumbprint-list "${thumbprint}" \
    --tags Key=Project,Value=alfaka Key=Environment,Value=dev Key=ManagedBy,Value=script >/dev/null
  echo "created: ${OIDC_PROVIDER_ARN}"
}

tmpdir="$(mktemp -d)"
trap 'rm -rf "${tmpdir}"' EXIT

policy_doc="${tmpdir}/policy.json"
trust_doc="${tmpdir}/trust.json"

cat > "${policy_doc}" <<JSON
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "secretsmanager:GetSecretValue",
        "secretsmanager:DescribeSecret"
      ],
      "Resource": [
        ${SECRET_RESOURCE_LINES}
      ]
    },
    {
      "Effect": "Allow",
      "Action": [
        "s3:ListBucket"
      ],
      "Resource": "${S3_BUCKET_ARN}"
    },
    {
      "Effect": "Allow",
      "Action": [
        "s3:GetObject",
        "s3:PutObject"
      ],
      "Resource": "${S3_BUCKET_ARN}/*"
    }
  ]
}
JSON

cat > "${trust_doc}" <<JSON
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": {
        "Federated": "${OIDC_PROVIDER_ARN}"
      },
      "Action": "sts:AssumeRoleWithWebIdentity",
      "Condition": {
        "StringEquals": {
          "${OIDC_PROVIDER_URL}:sub": "system:serviceaccount:${NAMESPACE}:${SERVICE_ACCOUNT}",
          "${OIDC_PROVIDER_URL}:aud": "sts.amazonaws.com"
        }
      }
    }
  ]
}
JSON

ensure_oidc_provider

if aws iam get-policy --policy-arn "${POLICY_ARN}" >/dev/null 2>&1; then
  aws iam create-policy-version \
    --policy-arn "${POLICY_ARN}" \
    --policy-document "file://${policy_doc}" \
    --set-as-default >/dev/null
  echo "updated policy: ${POLICY_ARN}"
else
  aws iam create-policy \
    --policy-name "${POLICY_NAME}" \
    --policy-document "file://${policy_doc}" \
    --tags Key=Project,Value=alfaka Key=Environment,Value=dev Key=ManagedBy,Value=script >/dev/null
  echo "created policy: ${POLICY_ARN}"
fi

if aws iam get-role --role-name "${ROLE_NAME}" >/dev/null 2>&1; then
  aws iam update-assume-role-policy \
    --role-name "${ROLE_NAME}" \
    --policy-document "file://${trust_doc}" >/dev/null
  echo "updated role trust: ${ROLE_ARN}"
else
  aws iam create-role \
    --role-name "${ROLE_NAME}" \
    --assume-role-policy-document "file://${trust_doc}" \
    --tags Key=Project,Value=alfaka Key=Environment,Value=dev Key=ManagedBy,Value=script >/dev/null
  echo "created role: ${ROLE_ARN}"
fi

aws iam attach-role-policy --role-name "${ROLE_NAME}" --policy-arn "${POLICY_ARN}" >/dev/null
echo "attached policy: ${POLICY_ARN}"
echo "${ROLE_ARN}"
