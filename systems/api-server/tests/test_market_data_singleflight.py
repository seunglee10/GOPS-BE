import unittest

from app.market_data.derived.service import DerivedCalculationService
from app.market_data.fill.service import DistributedFillSingleflight, parse_singleflight_value
from market_data.serving.chart_derived_data import build_volume_profile_request


class RedisSingleflightOwnershipTest(unittest.TestCase):
    def test_expired_derived_owner_cannot_delete_new_owner_lock(self):
        redis = _ExpiringRedis()
        service = DerivedCalculationService(canonical_query=object(), redis_client=redis)
        request = _request()

        old_owner = service._acquire_lock(request)
        redis.advance(31)
        new_owner = service._acquire_lock(request)
        service._release_lock(request, old_owner)

        self.assertIsInstance(new_owner, str)
        self.assertNotEqual(old_owner, new_owner)
        self.assertEqual(redis.get(request["lockKey"]), new_owner)

    def test_expired_fill_owner_cannot_delete_or_complete_new_owner_lock(self):
        redis = _ExpiringRedis()
        singleflight = DistributedFillSingleflight(redis, lock_ttl_seconds=1, terminal_ttl_seconds=5)

        old_acquired, old_owner, _state = singleflight.acquire("request-1")
        redis.advance(1.1)
        new_acquired, new_owner, _state = singleflight.acquire("request-1")
        before = redis.get(singleflight._key("request-1"))

        self.assertTrue(old_acquired)
        self.assertTrue(new_acquired)
        self.assertFalse(singleflight.release("request-1", old_owner))
        self.assertFalse(singleflight.complete("request-1", old_owner, "failed"))
        self.assertEqual(redis.get(singleflight._key("request-1")), before)
        self.assertTrue(singleflight.complete("request-1", new_owner, "filled"))
        self.assertEqual(parse_singleflight_value(redis.get(singleflight._key("request-1")))["status"], "filled")

    def test_eval_failure_leaves_lock_untouched_for_ttl_expiry(self):
        redis = _ExpiringRedis(fail_eval=True)
        service = DerivedCalculationService(canonical_query=object(), redis_client=redis)
        request = _request()
        owner = service._acquire_lock(request)

        service._release_lock(request, owner)

        self.assertEqual(redis.get(request["lockKey"]), owner)


class _ExpiringRedis:
    def __init__(self, *, fail_eval=False):
        self.now = 0.0
        self.values = {}
        self.fail_eval = fail_eval

    def advance(self, seconds):
        self.now += seconds

    def get(self, key):
        self._expire(key)
        item = self.values.get(key)
        return item[0] if item else None

    def set(self, key, value, nx=False, ex=None):
        self._expire(key)
        if nx and key in self.values:
            return False
        expires_at = self.now + float(ex) if ex is not None else None
        self.values[key] = (value, expires_at)
        return True

    def eval(self, script, numkeys, key, *args):
        del script
        if self.fail_eval:
            raise RuntimeError("eval unavailable")
        if numkeys != 1:
            raise AssertionError("fixture supports one key")
        current = self.get(key)
        expected = args[0]
        if current != expected:
            return 0
        if len(args) == 1:
            self.values.pop(key, None)
            return 1
        replacement, ttl_seconds = args[1], args[2]
        self.values[key] = (replacement, self.now + float(ttl_seconds))
        return 1

    def _expire(self, key):
        item = self.values.get(key)
        if item and item[1] is not None and item[1] <= self.now:
            self.values.pop(key, None)


def _request():
    return build_volume_profile_request(
        symbol="AAPL",
        interval="1m",
        from_time="2026-07-08T13:30:00.000Z",
        to_time="2026-07-08T14:00:00.000Z",
        price_bin_size="auto",
        target_bins=10,
        price_min=None,
        price_max=None,
    )


if __name__ == "__main__":
    unittest.main()
