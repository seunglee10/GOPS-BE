from __future__ import annotations

from typing import Any


COMPARE_AND_DELETE_LUA = """
local current = redis.call('GET', KEYS[1])
if current == ARGV[1] then
  return redis.call('DEL', KEYS[1])
end
return 0
""".strip()

COMPARE_AND_SET_EX_LUA = """
local current = redis.call('GET', KEYS[1])
if current == ARGV[1] then
  redis.call('SET', KEYS[1], ARGV[2], 'EX', ARGV[3])
  return 1
end
return 0
""".strip()


def compare_and_delete(redis_client: Any, key: str, expected_value: str) -> bool:
    try:
        return int(redis_client.eval(COMPARE_AND_DELETE_LUA, 1, key, expected_value) or 0) == 1
    except Exception:
        return False


def compare_and_set_ex(
    redis_client: Any,
    key: str,
    expected_value: str,
    replacement_value: str,
    ttl_seconds: int,
) -> bool:
    try:
        return int(redis_client.eval(
            COMPARE_AND_SET_EX_LUA,
            1,
            key,
            expected_value,
            replacement_value,
            max(1, int(ttl_seconds)),
        ) or 0) == 1
    except Exception:
        return False
