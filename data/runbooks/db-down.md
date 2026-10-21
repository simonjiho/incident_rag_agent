# Runbook: DB Down 대응 절차

## 증상
- 애플리케이션 로그에 `Connection refused`, `could not connect to server`가 모든 인스턴스에서 동시에 반복된다
- DB 포트(3306/5432) 헬스체크가 응답하지 않고 mysqld/postgres 프로세스가 내려가 있다
- 모니터링에서 DB 노드 상태가 `down`이고 활성 커넥션 수가 0으로 떨어진다
- 읽기와 쓰기가 모두 즉시 실패하며 `SQLSTATE 08006` 같은 연결 오류가 난다 (느려지는 것이 아니라 바로 실패)
- 커넥션 풀 고갈이나 락 대기와 달리 DB 접속 자체가 거부되고, 호스트의 CPU·디스크 지표는 정상이다
- `primary node unreachable` 알림과 함께 failover가 시작됐거나 승격할 대기 노드가 없다
- 복제본도 `Lost connection to master`로 복제 스레드가 멈춘다 (복제 지연이 아니라 연결 단절)

## 조치 절차
1. 모니터링 알람으로 장애를 즉시 인지한다 (get_monitoring_status로 현재 상태 확인).
2. 커넥션 풀/리플리케이션 상태를 점검하고 필요 시 failover 노드로 전환한다.
3. 서비스 정상화 확인 후 원인 분석과 재발 방지 대책을 문서화한다.
