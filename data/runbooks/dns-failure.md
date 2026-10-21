# Runbook: DNS 해석 실패(DNS Failure) 대응 절차

## 증상
- `Temporary failure in name resolution`, `getaddrinfo EAI_AGAIN`, `NXDOMAIN` 오류가 반복된다
- 호스트명으로는 실패하지만 같은 서버에 IP로 직접 접속하면 성공한다 (네트워크 타임아웃과 구분)
- 쿠버네티스 내부 서비스 이름(`*.svc.cluster.local`) 조회가 `SERVFAIL`로 실패하고 CoreDNS 파드가 재시작 중이다
- `dig`/`nslookup`이 응답 없이 수 초 대기하거나 리졸버가 아예 응답하지 않는다
- 이미 맺어진 keep-alive 연결은 정상이고 새 연결을 만드는 요청만 실패한다
- DNS 레코드 변경이나 TTL 만료 직후 일부 리졸버만 옛 IP를 돌려준다
- 서로 무관한 여러 서비스가 서로 다른 도메인을 동시에 찾지 못한다
- `resolv.conf`의 nameserver나 `ndots` 설정이 최근 바뀌었다

## 조치 절차
1. get_monitoring_status로 어떤 서비스에서 이름 해석 오류가 발생하는지 확인하고, 같은 호스트를 IP로 직접 호출했을 때 성공하는지 비교한다.
2. 문제 파드나 서버에서 `dig`/`nslookup`으로 리졸버 응답(SERVFAIL, NXDOMAIN, 무응답)을 확인하고 /etc/resolv.conf가 올바른지 본다.
3. 쿠버네티스라면 CoreDNS 파드 상태와 로그를 확인하고, 비정상이면 재시작하거나 레플리카를 늘린다.
4. 최근 DNS 레코드 변경이나 TTL 만료가 있었다면 레코드를 원복하고, 임시로 hosts 등록이나 대체 리졸버로 우회한다.
5. 정상화 후 로컬 DNS 캐시와 재시도 설정을 점검하고, 리졸버 응답 지연과 실패율에 대한 알림을 추가한다.
