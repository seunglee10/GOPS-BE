from kis_trader.cli import main, run_smoke
from kis_trader.domain.status import OrderStatus


def test_in_memory_smoke_success_flow():
    result = run_smoke("success")

    assert result["status"] == OrderStatus.SUBMITTED.value
    assert result["published_topics"] == ["orders.commands.v1", "broker.submit-results.v1"]
    assert result["metrics"]["orders_total"] == 1


def test_cli_smoke_prints_json(capsys):
    code = main(["smoke", "--outcome", "timeout"])

    captured = capsys.readouterr()
    assert code == 0
    assert '"status": "SUBMIT_FAILED_UNKNOWN"' in captured.out
