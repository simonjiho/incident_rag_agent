# Runbook: 복제 지연(Replication Lag) 대응 절차

## 증상
- `Seconds_Behind_Master`나 `replica_lag_seconds`가 계속 늘고 복제 지연 경고가 뜬다
- 방금 쓴 주문이나 설정이 읽기 복제본에서 조회되지 않는다 (stale read)
- 주 노드는 정상이고 쓰기도 성공한다 (DB 다운과 구분)
- 에러는 없는데 읽기 전용 대시보드와 쓰기 화면의 데이터가 서로 다르다
- `pg_stat_replication`의 `replay_lag`가 커지고 주 노드의 `pg_wal` 디렉터리가 불어난다
- 대량 UPDATE, DDL, 장시간 트랜잭션 직후에 지연이 시작됐다
- 복제 SQL(apply) 스레드는 Running이지만 따라잡는 속도가 유입보다 느리다
- 복제 I/O 스레드가 멈추고 연결이 끊긴 경우는 DB 다운 절차의 복제 단절에 해당한다

## 조치 절차
1. get_monitoring_status와 DB 지표로 복제 지연 시간(`Seconds_Behind_Master`, `replay_lag`)과 주 노드의 쓰기 성공 여부를 확인한다.
2. 복제 I/O 스레드와 SQL(apply) 스레드가 둘 다 Running인지 보고, 멈췄다면 에러 로그의 원인부터 해결한다.
3. 대량 UPDATE, DDL, 장시간 트랜잭션이 지연을 만들고 있으면 해당 작업을 중단하거나 작은 배치로 나눈다.
4. 지연이 해소될 때까지 최신성이 중요한 읽기는 주 노드로 돌리고 복제본은 읽기 풀에서 일시 제외한다.
5. 정상화 후 복제본 사양과 병렬 복제 설정을 점검하고, 지연 임계값 알림과 failover 후보 승격 기준을 재검토한다.
