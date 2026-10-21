# Runbook: 설정 오류(Config Error) 대응 절차

## 증상
- 배포나 설정 변경 직후에 시작됐고 직전 버전으로 롤백하면 바로 정상화된다
- 파드가 `CrashLoopBackOff`이고 로그에 `invalid configuration`, `missing required environment variable`, `failed to parse application.yml`이 보인다
- 인스턴스 전부가 동일하게 실패한다 (일부만 이상한 것이 아니다)
- ConfigMap, Secret, 환경변수, feature flag 값이 최근에 바뀌었다
- DB 자체는 살아 있는데 앱이 엉뚱한 호스트나 틀린 계정을 가리켜 `authentication failed`, `Unknown database`가 난다 (DB 다운과 구분)
- readiness probe가 실패해 새 릴리스에 트래픽이 붙지 않는다
- 스테이징에서는 정상이고 운영 환경에서만 재현된다
- 시작 시 설정 검증 단계(`startup validation failed`)에서 프로세스가 스스로 종료한다

## 조치 절차
1. get_monitoring_status로 장애 시작 시각과 영향 범위(모든 인스턴스인지 일부인지)를 확인하고 같은 시각의 배포·설정 변경 이력을 대조한다.
2. 파드/프로세스 시작 로그에서 `invalid value`, `missing required` 같은 설정 검증 오류와 잘못된 접속 대상(호스트, 포트, 계정)을 찾는다.
3. ConfigMap/Secret/환경변수/feature flag의 변경분을 직전 정상 버전과 diff하고 오류 값을 원복한다.
4. 원복이 어려우면 직전 정상 릴리스로 롤백하고 readiness probe가 통과하는지 확인한 뒤 트래픽을 복구한다.
5. 정상화 후 설정을 배포 전에 검증하는 스키마 체크를 CI에 추가하고, 카나리 배포로 설정 변경의 영향 범위를 줄인다.
