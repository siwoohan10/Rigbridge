# RigBridge — VRChat 월드 무전기 ↔ wfview(IC-7300)

월드 "My home"의 무전기(IC-7300 모형)를 실제 무전기와 연동합니다.

```
[월드 다이얼/버튼/PTT] → Udon Debug.Log("[RIGBRIDGE] {...}") → 운용자 VRChat 로그
        → RigBridge(로그 감시) → wfview rigctld(4533) → IC-7300
[IC-7300] → wfview rigctld(상태) + Realtek 스피커 루프백(수신 음성) → RigBridge → LCD 화면 + 음성 HLS
        → http://localhost:8810/radio.m3u8 (운용자) / Cloudflare 터널 주소 (친구)
        → 월드 무전기 LCD (AVPro) + 무전기 스피커
```

## 1. 한 번만 하는 준비

1. **wfview 설정** → *Settings → External Control* (또는 *LAN/Server* 탭)
   - **rigctld 서버 켜기**, 포트 `4533`
   - 수신 음성 출력 장치는 **스피커 / 헤드폰(Realtek)** 그대로 둡니다. RigBridge가 이 스피커로 나가는 소리를
     녹음(WASAPI 루프백)해서 월드로 보냅니다. VR 중 VRChat 소리는 Virtual Desktop 쪽으로 나가므로 섞이지 않지만,
     PC에서 Realtek 스피커로 다른 소리(유튜브 등)를 틀면 그 소리도 같이 나갑니다.
   - (참고) wfview 2.24의 TCI 서버는 LAN 연결에서 음성 프레임을 보내지 않아 쓰지 않습니다.
2. **VRChat 설정** → *Comfort & Safety* → **Allow Untrusted URLs 켜기**
   (운용자와 친구 모두. localhost·터널 주소의 스트림을 재생하려면 필요합니다.)
3. `config.json`
   - `operator_name`: 운용자 VRChat 표시 이름 (월드 쪽 `RadioController.operatorName`과 같아야 함)
   - `ptt_allowed`: 송신을 허용할 다른 사람의 VRChat 표시 이름 목록 (무선국 자격이 있는 사람만!)
     → 월드 쪽 `RadioController.pttAllowed`에도 같은 이름을 넣어야 합니다.
   - `world_id`: 이 월드 ID. 다른 월드에서는 명령을 받지 않습니다.
   - `audio_source`: `"loopback"`(기본, `audio_device`에 들어간 이름을 포함한 스피커의 소리를 녹음),
     `"tci"`(wfview TCI 음성), `"device"`(입력 장치에서 녹음, `audio_device`에 장치 이름)

## 2. 사용

1. wfview를 켜고 무전기에 연결합니다.
2. `start_rigbridge.bat` 실행 → 작업 표시줄 트레이에 무전기 아이콘이 생깁니다.
   - 아이콘 색: 회색 = 무전기 미연결, 하늘색 = 연결됨, 빨강 = 송신 중
3. VRChat에서 월드에 들어가면 무전기 LCD에 실제 무전기 화면이 나옵니다(약 3~4초 지연).
4. **친구와 같이 쓸 때**: 트레이 메뉴 → *친구용 주소 복사* → 월드의 무전기 옆 주소 입력칸에 붙여 넣고 *연결*.
   (Cloudflare 임시 터널이라 RigBridge를 켤 때마다 주소가 바뀝니다.)

## 3. 월드 조작

| 조작 | 동작 |
|---|---|
| VFO 다이얼 오른쪽/왼쪽 | 주파수 + / − (현재 스텝만큼) |
| STEP | 스텝 10 → 100 → 1k → 5k → 10k Hz |
| BAND ▲ / ▼ | 다음/이전 아마추어 밴드 시작 주파수 |
| MODE | LSB → USB → CW → AM → FM → RTTY → PKTUSB |
| 핸드 마이크 | PTT 켜기/끄기 (운용자·허용 목록만) |
| 무전기 윗면 스피커 그릴 | 무전기 소리 켜기/끄기 (누른 사람에게만) |
| LCD 오른쪽 맨 위 키 | 안테나 튜너(ATU) 켜기/끄기 — LCD에 `ATU` 표시 |
| LCD 오른쪽 둘째 키 | 튜닝 시작 (짧게 송신하므로 PTT와 같은 권한) — 튜닝 중 LCD에 `TUNE` |

튜닝 시작은 wfview TCI(`tune:2;` = CI-V `1C 01 02`)로 보냅니다. wfview rigctld에는 튜닝 명령이 없습니다
(`G TUNE`은 wfview에서 VFO A 선택으로 처리됨). 4초 안에 무전기가 송신으로 바뀌지 않으면
`rigbridge.log`에 "tuning probably did not start" 경고가 남습니다.

## 4. 안전장치

- 설정한 월드(`world_id`)에 있을 때만 명령을 받습니다. 월드를 나가면 PTT가 자동으로 꺼집니다.
- PTT는 `ptt_timeout_sec`(기본 180초)가 지나면 자동으로 꺼집니다.
- RigBridge 종료, 무전기 연결 끊김 시에도 PTT를 끕니다.
- 월드와 RigBridge 양쪽에서 송신 허용 이름을 확인합니다. 보낸 사람 이름은 VRChat이 보증하는 값입니다.
- 송신 음성은 wfview의 TX 입력 장치(현재: VR 헤드셋 마이크)입니다. 친구가 PTT를 눌러도 송신되는 소리는
  운용자 마이크입니다.

### 원격 무전기(라즈베리 파이) 연결이 끊길 때

무전기는 라즈베리 파이(wfview 서버)에 USB로 연결되어 있고, 이 PC의 wfview는 네트워크로 파이에 붙습니다.
파이와의 연결이 끊겨도 wfview rigctld는 마지막 값을 계속 돌려주기 때문에, RigBridge가 파이를 따로 감시합니다.

- 파이 주소는 wfview 설정(LAN → IP 주소)에서 자동으로 읽습니다. 유선 LAN으로 바꿔 주소가 달라져도
  wfview에서만 바꾸면 됩니다. 직접 정하려면 `config.json`의 `rig_link_host`에 적습니다.
- 1초마다 ping을 보내고, 3번 연속 실패하면 "연결 끊김"으로 봅니다(2번 연속 성공하면 복구).
- 끊긴 동안에는 월드 조작을 무시합니다(복구될 때 한꺼번에 실행되지 않도록). LCD에 `RIG LINK LOST`,
  트레이 아이콘은 주황색으로 바뀝니다.
- 송신 중에 끊기면 PTT 끄기를 보내고, 복구된 뒤에도 `T 0`을 1초 간격으로 3번 더 보내 확실히 끕니다.
  그동안 새 PTT는 받지 않습니다.
- ping은 네트워크만 확인합니다. 파이의 wfview 서버만 멈춘 경우는 감지하지 못하므로
  **IC-7300 자체의 송신 시간 제한(Time-Out Timer)을 켜 두는 것을 권장합니다.**

### 지연 (운용자 RTSP / 친구 HLS)

- **운용자**: RigBridge가 `mediamtx/`의 MediaMTX(v1.21.3, 이 PC 안에서만 열림: 127.0.0.1:8554)를 함께 켜고
  `rtsp://localhost:8554/radio`로 저지연 스트림을 보냅니다(약 1초). 월드는 운용자에게 이 주소를 먼저 쓰고,
  안 되면 HLS(`http://localhost:8810/radio.m3u8`)로 자동 전환합니다.
- **친구**: Cloudflare 터널은 HTTP만 되므로 HLS(0.6초 조각 × 4)로 봅니다(약 3초).
- 끄려면 `config.json`에 `"rtsp_enabled": false`.

### 수신 음성 장치 바꾸기

트레이 메뉴 → **수신 음성 장치 고르기**에서 무전기 소리를 가져올 장치를 고릅니다(바로 적용, config.json에 저장).
- **스피커 출력**: 그 스피커로 나가는 소리를 녹음 — wfview의 출력 장치로 지정한 스피커를 고르세요.
- **입력 장치**: 마이크/라인 입력/가상 오디오 케이블 등에서 녹음.
- 각 항목 옆 "소리 0.xxx"가 그 장치의 지금 소리 크기입니다. 무전기 소리가 나는 동안
  "소리 크기 다시 재기"를 누르면 어느 장치에 소리가 실리는지 알 수 있어요.
- ※ 표시(기본 출력)는 VRChat 소리도 같이 나가는 스피커라, 고르면 VRChat 소리까지 섞여 월드에서 메아리가 생길 수 있어요.
  wfview 출력을 VRChat과 다른 장치로 두는 걸 권장합니다.

### 한 번에 하나만 실행

RigBridge는 하나만 실행됩니다(두 번째로 켜면 "이미 실행 중" 창이 뜨고 꺼짐). 여러 개가 동시에 돌면 스트림이
깨지고 조작이 여러 번 들어가기 때문입니다. 파이 IP를 바꾸는 등 설정을 다시 읽히려면
트레이 메뉴 → **다시 시작**을 쓰세요.

## 유튜브 자막 (침실 모니터 + 거실 TV)

VizVid로 유튜브 영상(직접 주소 또는 방의 유튜브 검색)을 틀면, 침실 모니터와 거실 TV 위에 유튜브 자막이 유튜브가 정한 위치대로 뜹니다.

- 꾸민 자막도 반영: 글자 색·투명도, 굵게/기울임, 글자 크기, 글꼴(명조·고정폭·손글씨·필기체 — PC에 깔린 Windows 글꼴 사용), 테두리·그림자, 배경색.
- 여러 위치에 동시에 뜨는 자막은 화면마다 최대 8곳까지 각각 표시. 테두리용·그림자용으로 같은 줄을 두 번 깐 자막은 한 줄로 합침.
- 밑줄은 VRChat 글자(Unity Text)가 지원하지 않아 빠짐.

- 운용자 VRChat이 지금 재생 주소를 로그로 알리면 → RigBridge가 yt-dlp로 자막을 받아 `http://localhost:8810/cap.json`
  (이 PC에서만, 터널로는 안 열림)으로 내주고 → 운용자 화면이 재생 시간에 맞는 줄을 골라 방 사람 모두에게 동기화합니다.
- 그래서 **운용자(RigBridge)가 방에 있을 때만** 자막이 나옵니다.
- 자막 고르는 순서: 한국어 자막 → 한국어 자동 자막/자동 번역 → 다른 언어 자막 → 원어 자동 자막. 자막이 없는 영상은 표시 안 함.
- 모니터 오른쪽 아래 **CC** 버튼: 내 화면에서만 자막 켜기/끄기.
- RigBridge에는 영상 ID만 넘어가고(주소를 그대로 yt-dlp에 넘기지 않음), 유튜브·`api.u2b.cx/vrcurl` 주소만 처리합니다.
- 끄려면 `config.json`에 `"captions_enabled": false`.

## 5. 문제 해결

- 로그: `rigbridge.log`
- 상태 확인: http://localhost:8810/status.json
- LCD에 `RIG OFFLINE` → wfview rigctld 서버가 꺼져 있거나 포트가 다름
- LCD에 `RIG LINK LOST` → 라즈베리 파이에 ping이 안 감 (와이파이 끊김 등). 복구되면 자동으로 돌아옵니다.
- LCD에 `no audio` → `audio_device`의 스피커를 찾지 못함 (화면과 조작은 정상 동작)
- 화면이 멈췄을 때 → 월드의 LCD를 누르면 내 화면에서 다시 연결 (재생이 15초 멈추면 자동으로도 다시 연결)
- 소리만 안 남 → wfview에서 무전기 소리가 PC의 Realtek 스피커로 나오는지 확인 (RigBridge는 그 소리를 녹음함)
- 월드 LCD가 파란 기본 화면 그대로 → 스트림이 아직 안 들어옴. RigBridge가 켜져 있는지, VRChat의
  Allow Untrusted URLs가 켜져 있는지 확인 (실패하면 10초마다 다시 시도)
