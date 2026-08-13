# 역할: pod/job 시작 시 필수 런타임 설정값을 검증합니다.
# 사용: AWS placeholder나 빈 값으로 조용히 실행되는 것을 막습니다.


def has_placeholder_value(value):
    if value is None:
        return False
    if isinstance(value, (list, tuple, set)):
        return any(has_placeholder_value(item) for item in value)
    text = str(value)
    return "YOUR_" in text or "REPLACE_" in text


def validate_required_values(component, values):
    errors = []
    for name, value in values.items():
        if is_empty_value(value):
            errors.append(f"{name} is empty")
            continue
        if has_placeholder_value(value):
            errors.append(f"{name} contains placeholder value {value!r}")
    if errors:
        raise RuntimeError(f"Invalid {component} runtime config: " + "; ".join(errors))


def is_empty_value(value):
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip() == ""
    if isinstance(value, (list, tuple, set)):
        return len(value) == 0 or any(is_empty_value(item) for item in value)
    return False
