# Runbook: 인증서 만료(Certificate Expiry) 대응 절차

## 증상
- `x509: certificate has expired or is not yet valid` 오류가 클라이언트 로그에 찍힌다
- 브라우저에 `NET::ERR_CERT_DATE_INVALID` 경고가 표시되고 API 클라이언트가 HTTPS 호출을 거부한다
- 만료 시각을 지나는 순간부터 모든 HTTPS 요청이 한꺼번에 실패한다 (점진적으로 나빠지지 않는다)
- TCP 연결은 맺어지고 TLS 핸드셰이크 단계에서만 끊긴다 (네트워크 경로나 DNS 문제가 아니다)
- 서비스 간 mTLS 호출에서 `tlsv1 alert certificate expired` 또는 gRPC `UNAVAILABLE: ssl handshake failed`가 난다
- cert-manager의 `CertificateNotReady` 또는 ACME `renewal failed` 로그가 며칠 전부터 쌓여 있었다
- 인증서 만료 D-7, D-1 알림을 놓쳤거나 아예 등록돼 있지 않았다
- 서버 자원과 DB는 정상이고 헬스체크만 TLS 오류로 실패한다

## 조치 절차
1. get_monitoring_status로 TLS 핸드셰이크 실패율과 영향받는 엔드포인트를 확인하고, `openssl s_client`로 인증서 만료일(notAfter)을 직접 확인한다.
2. 만료된 인증서가 어느 계층(로드밸런서, 인그레스, 서비스 메시, 내부 mTLS)에 걸려 있는지 찾고 체인에 중간 인증서가 빠지지 않았는지 점검한다.
3. 새 인증서를 즉시 발급하거나 갱신해 해당 계층에 교체 배포하고, 인그레스/프록시를 reload해 새 인증서를 읽게 한다.
4. 자동 갱신(ACME, cert-manager)이 왜 실패했는지 로그를 확인해 갱신 권한, DNS 챌린지, 스케줄러 문제를 바로잡는다.
5. 정상화 후 전체 인증서 인벤토리를 만료일 기준으로 점검하고, 만료 30일·7일 전 알림을 등록한다.
