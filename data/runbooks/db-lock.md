# Runbook: DB 락 경합(DB Lock Contention) 대응 절차

## 증상
- MySQL `ER_LOCK_WAIT_TIMEOUT` (1205) 또는 PostgreSQL `deadlock detected`, `canceling statement due to lock timeout`이 반복된다
- DB는 접속과 응답이 정상이고 단순 SELECT는 대부분 통과하지만 특정 테이블의 쓰기만 멈춘다 (DB 다운과 구분)
- `pg_stat_activity`에 `idle in transaction` 세션이 오래 남아 있고 그 뒤로 대기 세션이 줄지어 있다
- 활성 쿼리는 많은데 DB의 CPU와 디스크 I/O는 낮다 (대부분 락을 기다리는 중)
- 끝나지 않는 배치 UPDATE나 오래 열린 트랜잭션이 최상위 블로커로 보인다
- `Waiting for table metadata lock` 상태의 세션들이 ALTER TABLE 같은 DDL 뒤에 쌓인다
- 핑과 TCP 연결은 정상이고 오류 메시지가 락 관련이다 (패킷 손실이 있는 네트워크 타임아웃과 구분)
- 애플리케이션이 커넥션을 반환하지 못해 커넥션 수만 서서히 늘어난다

## 조치 절차
1. get_monitoring_status로 DB 응답은 정상인지, 쓰기 요청이 대기열에 쌓이기 시작한 시점이 언제인지 확인한다.
2. `pg_stat_activity`나 `SHOW ENGINE INNODB STATUS`로 블로킹 세션과 대기 세션의 체인을 찾아 최상위 블로커를 특정한다.
3. 오래 열린 `idle in transaction` 세션이나 끝나지 않는 배치 UPDATE는 담당자 확인 후 `KILL` 또는 `pg_terminate_backend`로 종료한다.
4. 진행 중인 DDL이나 마이그레이션이 메타데이터 락을 잡고 있으면 중단하고, 점검 시간대에 다시 수행하도록 일정을 조정한다.
5. 정상화 후 트랜잭션을 짧게 유지하도록 쿼리를 수정하고, 락 대기 시간 상한과 긴 트랜잭션 알림을 설정한다.
