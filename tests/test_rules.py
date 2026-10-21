"""agent/rules.py — 시스템명 추출과 장애 유형 분류 규칙 테스트."""
import pytest

from agent import rules
from agent.rules import (INCIDENT_TYPE_RULES, KNOWN_SYSTEMS, classify_incident_type,
                         extract_system_name)

ORIGINAL_SYSTEMS = {"auth-service", "web-01", "payments-service", "cache-service",
                    "gateway-service", "log-service"}


def monitoring(status: str, *alerts: str) -> dict:
    """collected 항목 하나를 만든다 (research 노드가 쌓는 모양과 같다)."""
    return {"source": "monitoring", "data": {"status": status, "alerts": list(alerts)}}


def test_known_systems_include_original_six():
    # 시스템 총 개수는 data/monitoring.json 편집에 따라 늘 수 있어 단언하지 않는다
    assert ORIGINAL_SYSTEMS <= KNOWN_SYSTEMS


@pytest.fixture
def three_systems(monkeypatch):
    """monitoring.json 내용과 무관하게 매칭 로직만 검증하려고 고정한 시스템 집합."""
    monkeypatch.setattr(rules, "KNOWN_SYSTEMS", {"auth-service", "web-01", "payments-service"})


def test_extract_picks_earliest_mentioned_system(three_systems):
    assert extract_system_name("payments-service 가 느리고 auth-service 도 이상해요") == "payments-service"
    assert extract_system_name("auth-service 와 payments-service 장애") == "auth-service"


def test_extract_single_mention(three_systems):
    assert extract_system_name("web-01 서버 CPU 확인해줘") == "web-01"


def test_extract_falls_back_to_auth_service(three_systems):
    assert extract_system_name("뭔가 이상해요") == "auth-service"
    assert extract_system_name("billing-service 응답이 느려요") == "auth-service"


@pytest.mark.parametrize("alerts, expected", [
    (["p95 latency 2.4s (threshold 1.0s)", "connection pool 92% used"], "high-latency"),
    (["connection pool 92% used"], "high-latency"),
    (["CPU 91% (threshold 80%)", "payments-service-3 instance responding slow"], "cpu-spike"),
    (["memory usage climbing steadily over 6h", "heap near limit"], "memory-leak"),
    (["upstream timeout to payments-service (504)", "network packet loss 3%"], "network-timeout"),
    (["disk usage 95% on /var/log", "disk space low"], "disk-full"),
])
def test_classify_original_style_alerts(alerts, expected):
    assert classify_incident_type([monitoring("degraded", *alerts)]) == expected


def test_classify_status_down_is_db_down():
    assert classify_incident_type([monitoring("down")]) == "db-down"


def test_classify_no_match_falls_back_to_db_down():
    assert classify_incident_type([monitoring("healthy")]) == "db-down"
    assert classify_incident_type([monitoring("degraded", "something odd")]) == "db-down"
    assert classify_incident_type([]) == "db-down"


def test_classify_first_rule_wins():
    # latency 규칙이 cpu 규칙보다 앞에 있으므로 둘 다 걸리면 high-latency
    assert classify_incident_type(
        [monitoring("degraded", "CPU 91%", "p95 latency 2.4s")]) == "high-latency"


def test_classify_ignores_non_monitoring_sources():
    collected = [{"source": "runbook", "data": {"status": "", "alerts": ["disk full"]}},
                 monitoring("degraded", "CPU 91% (threshold 80%)")]
    assert classify_incident_type(collected) == "cpu-spike"


def test_classify_uses_all_monitoring_results():
    # web-01은 정상, 두 번째 시스템의 알림이 유형을 결정한다
    collected = [monitoring("healthy"), monitoring("degraded", "disk usage 95%")]
    assert classify_incident_type(collected) == "disk-full"


def test_rules_table_maps_to_six_runbooks():
    assert {t for _, t in INCIDENT_TYPE_RULES} == {
        "high-latency", "cpu-spike", "memory-leak", "network-timeout",
        "disk-full", "db-down"}
