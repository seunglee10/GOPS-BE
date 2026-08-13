#!/usr/bin/env bash
# 역할: k8s 매니페스트의 ${AWS_ACCOUNT_ID} 자리표시자를 실제 값으로 바꿉니다.
#
# 배경: AWS 계정 ID를 저장소에 하드코딩하지 않기 위해 매니페스트에는 ${AWS_ACCOUNT_ID}
# 자리표시자만 두고, kubectl apply 직전에 이 스크립트로 해석합니다.
#
# 주의: 파일을 제자리에서 수정합니다. CI 체크아웃이나 deploy-dev-local.sh 가 만드는
# 임시 worktree처럼 일회용 작업 트리에서만 실행하세요.
#
# envsubst 에 '${AWS_ACCOUNT_ID}' 만 넘기는 이유: 매니페스트 안에는 컨테이너가 실행 시점에
# 쓰는 셸 변수(${topic}, ${CLUSTER_ID} 등)가 있고, 변수를 한정하지 않으면 그것들이 빈
# 문자열로 지워집니다.
set -euo pipefail

K8S_ROOT="${K8S_ROOT:-infra/k8s}"
AWS_ACCOUNT_ID="${AWS_ACCOUNT_ID:?AWS_ACCOUNT_ID를 넣어주세요. 예) export AWS_ACCOUNT_ID=\"$(aws sts get-caller-identity --query Account --output text 2>/dev/null || echo '<aws-account-id>')\"}"
export AWS_ACCOUNT_ID

if ! command -v envsubst >/dev/null 2>&1; then
  printf 'envsubst 가 필요합니다. (Ubuntu: apt-get install gettext-base, macOS: brew install gettext)\n' >&2
  exit 1
fi

if [[ ! -d "${K8S_ROOT}" ]]; then
  printf '경로를 찾을 수 없습니다: %s\n' "${K8S_ROOT}" >&2
  exit 1
fi

resolved=0
while IFS= read -r manifest; do
  if ! grep -q '\${AWS_ACCOUNT_ID}' "${manifest}"; then
    continue
  fi
  tmp_file="$(mktemp)"
  envsubst '${AWS_ACCOUNT_ID}' < "${manifest}" > "${tmp_file}"
  mv "${tmp_file}" "${manifest}"
  resolved=$((resolved + 1))
  printf 'resolved: %s\n' "${manifest}"
done < <(find "${K8S_ROOT}" -type f \( -name '*.yaml' -o -name '*.yml' \) | sort)

if [[ "${resolved}" -eq 0 ]]; then
  printf '${AWS_ACCOUNT_ID} 자리표시자를 찾지 못했습니다. 이미 해석되었는지 확인하세요.\n' >&2
  exit 1
fi

if grep -rq '\${AWS_ACCOUNT_ID}' "${K8S_ROOT}"; then
  printf '해석되지 않은 자리표시자가 남아 있습니다: %s\n' "${K8S_ROOT}" >&2
  exit 1
fi

printf '%s개 매니페스트의 AWS 계정 ID를 해석했습니다.\n' "${resolved}"
