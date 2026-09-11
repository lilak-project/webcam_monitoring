# BRIO 500 화면 OCR 모니터

Logitech BRIO 500으로 모니터/장비 화면을 주기적으로 촬영하고, 지정 영역(ROI)을
전처리한 뒤 Tesseract OCR 결과를 CSV 또는 InfluxDB에 기록하는 Linux/macOS용
파이프라인입니다.

## macOS에서 실행

```bash
brew install ffmpeg tesseract
```

Homebrew 설치가 어려운 구형 macOS에서는 Anaconda로 프로젝트 전용 환경을
만들 수 있습니다. 실행 스크립트가 `.runtime/bin`을 자동으로 사용합니다.

```bash
conda create -p "$PWD/.runtime" -c defaults --override-channels python=3.11 ffmpeg tesseract -y
```

`config.json`의 `camera`에 다음 값을 설정합니다. 해상도와 FPS는 기존 값을 유지합니다.

```json
"backend": "avfoundation",
"device": "Brio 500",
"pixel_format": "uyvy422",
"controls": {}
```

카메라 이름이나 번호는 `ffmpeg -f avfoundation -list_devices true -i ""`로
확인할 수 있습니다. macOS에서는 `/dev/video0` 및 V4L2 제어를 사용하지 않습니다.
카메라 접근 허용 창이 나타나면 허용하세요.

```bash
./scripts/server-start.sh
./scripts/server-stop.sh
```

시작 스크립트는 서버를 백그라운드에서 실행합니다. 실행 로그는
`data/web-service.log`, 프로세스 번호는 `data/web-service.pid`에 저장됩니다.
터미널에서 직접 실행 상태를 보고 싶다면 `./scripts/web-service.sh`를 사용합니다.

브라우저에서 http://localhost:8080 에 접속합니다. 별도 영상 창은
`./scripts/live-view.sh`로 열 수 있습니다. 웹 서비스와 동시에 실행하지 마세요.
입력 옵션은 [FFmpeg AVFoundation 문서](https://www.ffmpeg.org/ffmpeg-devices.html#avfoundation)를 참고하세요.

## 먼저 확인할 것

### RAON 화면 자동 판독과 위치 보정

`display_monitor.enabled: true` 설정에서는 웹 서버가 같은 카메라 프레임을 공유해
설정한 간격으로 Beam, ISOL 에너지·전류, Chopper/ISOL 반복률·펄스 폭,
Attenuator/Single bunch/Stripper 상태를 읽습니다. 대시보드 상단에서 자동 판독을
중지하거나 즉시 실행하고 CSV를 내려받을 수 있습니다. 사진 저장 주기와 값 분석·기록
주기는 각각 2~86400초 범위에서 독립적으로 설정할 수 있습니다. 설정은 `config.json`에
저장되어 재시작 후에도 유지됩니다. 자동 사진은 날짜별 `data/snapshots/YYYYMMDD/`에
`full-날짜-시간.jpg` 전체 화면과 `selected-날짜-시간.jpg` 원근 보정 영역을 한 쌍으로 저장합니다.
판독이 간격보다 오래 걸리면
완료 후 다음 판독을 시작하며 작업은 중첩하지 않습니다.

추가 패키지 설치:

```bash
.runtime/bin/python3 -m pip install --only-binary=:all: 'opencv-python-headless==4.8.1.78' 'numpy<2'
```

`config.display.example.json`은 현재 배치에 대한 설정 예시입니다. 기준 사진은
`data/screen-reference.jpg`, 화면 네 모서리 좌표는 `display_monitor.corners`
(좌상→우상→우하→좌하), 개별 판독 영역은 정면으로 보정한 1400×600 화면 좌표입니다.
다른 설치에서는 자기 카메라의 기준 사진과 좌표를 설정해야 합니다.

대시보드에서 **기울기 보정 영역 선택**을 누르고 좌상→우상→우하→좌하 순서로
네 모서리를 찍으면 비대칭 사각형을 정면 화면으로 펼쳐 미리 볼 수 있습니다.
**보정 영역 저장**은 선택 좌표와 그 순간의 원본 프레임을 새 추적 기준으로 저장합니다.

특징점 일치와 원근 보정으로 작은 이동·회전·크기 변화를 보정합니다. 기본 허용치는
기준 사진 대비 모서리 이동 100픽셀입니다. 보정 실패, 화면 이탈, 5초 이상 오래된
카메라 프레임은 실패로 기록하며 이전 값을 재사용하지 않습니다. 위치가 크게 바뀌면
기준 사진과 모서리 좌표를 다시 설정하고 웹 서비스를 재시작하세요.

각 항목은 세 가지 전처리 결과 중 최소 두 개의 신뢰도 기준을 통과한 값이 일치해야
유효합니다. 숫자만으로 불확실한 경우 항목 이름을 포함한 추가 영역으로 재검증합니다. 값이 서로 다르거나 소수점이 누락되면 ‘확인 필요’로 표시하고 CSV 값은
빈칸으로 남깁니다. 단위는 설정값이며 원소/동위원소 위첨자는 자동 판독 대상에서
제외합니다. 이 검증은 OCR 오독 가능성을 완전히 없애지는 않습니다.

기록은 `data/display-readings.csv`, 원본·보정 이미지와 개별 OCR 결과는
`data/display/<시각>/result.json`에 저장합니다. CSV는 계속 유지하며 이미지 증거는
`keep_artifacts`에 설정한 최근 20회만 유지하며 대시보드에서 사진을 확인할 수 있습니다. 기본 설정으로 서버를 다시
실행하면 자동 판독도 시작합니다.

위치 보정 구현은 [OpenCV 특징점과 Homography 문서](https://docs.opencv.org/4.10.0/d7/dff/tutorial_feature_homography.html)의 방식을 사용합니다.

### Linux USB 연결 확인

현재 구성에서는 USB-C 변환 어댑터의 접촉 불량 가능성이 가장 큽니다. 자동화를
시작하기 전에 아래 명령에서 `046d:0943`과 `/dev/video0`이 안정적으로 유지되는지
확인하세요.

```bash
watch -n 1 'lsusb | grep 046d:0943; ls -l /dev/video* 2>/dev/null'
```

가능하면 변환 어댑터 없이 연결하거나, 품질이 확인된 어댑터/케이블로 먼저
교체하는 것을 권장합니다.

## 설치

Ubuntu에서 필요한 시스템 도구를 설치합니다.

```bash
sudo apt update
sudo apt install ffmpeg tesseract-ocr v4l-utils
```

Python 3.10 이상만 있으면 별도 Python 패키지는 필요하지 않습니다.

```bash
cd /home/jungwoo/ko2520/webcam_monitoring
cp config.example.json config.json
python3 -m brio_ocr_monitor doctor --config config.json
```

## 카메라 구도와 설정

먼저 실시간 영상을 열어 구도, 초점, 노출을 확인할 수 있습니다.

```bash
./scripts/live-view.sh
```

다른 장치나 낮은 해상도를 사용할 때는 옵션을 지정합니다.

```bash
./scripts/live-view.sh -d /dev/video1 -s 1280x720 -r 30
```

종료하려면 영상 창에서 `q` 또는 `Esc`를 누릅니다. 화면을 좌우 반전하려면
`--flip-horizontal` 옵션을 추가하세요.

먼저 카메라가 지원하는 제어 항목과 현재값을 확인합니다.

```bash
v4l2-ctl -d /dev/video0 --list-ctrls-menus
```

자동 노출과 자동 초점을 끄고 값을 조절하는 예시는 다음과 같습니다. 카메라가
보고하는 범위 안에서만 값을 사용해야 합니다.

```bash
v4l2-ctl -d /dev/video0 \
  --set-ctrl=exposure_auto=1 \
  --set-ctrl=exposure_absolute=80 \
  --set-ctrl=focus_automatic_continuous=0 \
  --set-ctrl=focus_absolute=40
```

BRIO 500의 드라이버가 노출 항목에 다른 이름/값을 제공할 수 있으므로 위 명령을
그대로 가정하지 말고 `--list-ctrls-menus` 결과를 기준으로 `config.json`의
`camera.controls`를 수정하세요. 화면의 흰 영역이 날아가지 않을 때까지 노출을
낮추고, 글자 가장자리가 선명해지는 곳에 초점을 고정합니다.

## ROI 맞추기

1. 카메라를 화면에 가깝고 정면에 배치합니다.
2. 우선 전체 사진을 한 장 촬영합니다.
3. `config.json`의 `roi`를 `{ "x": 890, "y": 200, "width": 300,
   "height": 190 }`처럼 조정합니다.
4. 기존 사진으로 반복 시험하면 카메라를 다시 촬영하지 않아도 됩니다.

```bash
python3 -m brio_ocr_monitor run --config config.json \
  --input /home/c/censcounting0/shot.jpg
```

결과 이미지와 OCR 원문은 `data/artifacts/<시각>/`에, 한 줄 요약은
`data/readings.csv`에 저장됩니다. 현재 사진처럼 방 전체가 넓고 대상 화면이
과노출된 경우에는 전처리보다 구도와 노출을 먼저 고쳐야 합니다.

## 실행

### LAN 웹 대시보드

웹 서비스는 카메라 하나를 공유하여 라이브 영상, 사진 저장, OCR 실행, CSV 데이터
조회를 제공합니다. 라이브 영상에서 원하는 영역을 드래그하면 오른쪽에서 실시간으로
확대되며, **선택 영역을 OCR ROI로 저장** 버튼으로 이후 OCR 범위를 지정할 수 있습니다.
별도의 Python 패키지는 필요하지 않습니다.

```bash
./scripts/server-start.sh
```

서버의 IP 주소를 확인합니다.

```bash
hostname -I
```

같은 네트워크의 PC나 휴대전화에서 `http://192.168.1.x:8080`으로 접속하세요.
여기서 `192.168.1.x`는 위 명령에 표시된 **서버 자신의 주소**입니다. 서비스는
모든 인터페이스(`0.0.0.0`)에서 요청을 받습니다.

> 이 대시보드에는 아직 로그인 기능이 없습니다. 인터넷에 포트를 공개하지 말고
> 신뢰할 수 있는 내부망에서만 사용하세요. 웹 서비스 실행 중에는 카메라를 점유하는
> `live-view.sh`나 별도의 OCR `loop`를 동시에 실행하지 마세요.

포트를 변경하려면 다음처럼 실행합니다.

```bash
PORT=8090 ./scripts/server-start.sh
```

방화벽이 활성화되어 있다면 내부망에서만 포트를 허용합니다.

```bash
sudo ufw allow from 192.168.1.0/24 to any port 8080 proto tcp
```

### 명령행 실행

한 번 실행:

```bash
python3 -m brio_ocr_monitor run --config config.json
```

설정된 주기로 계속 실행:

```bash
python3 -m brio_ocr_monitor loop --config config.json
```

중단은 `Ctrl+C`입니다. OCR 텍스트가 비었거나 `validation.pattern`과 맞지 않으면
상태가 `ocr_failed`로 기록되고 원본/ROI/OCR 텍스트가 그대로 보존됩니다.

## 값 검증

숫자 하나를 읽는 화면이라면 다음과 같이 설정할 수 있습니다.

```json
"ocr": {
  "language": "eng",
  "psm": 7,
  "whitelist": "0123456789.+-"
},
"validation": {
  "pattern": "[-+]?\\d+(?:\\.\\d+)?"
}
```

여러 줄의 문자와 숫자가 있는 화면은 `psm: 6`, 빈 whitelist로 시작하는 편이
좋습니다. `--dpi 300`은 해상도 경고를 없애지만 흐림이나 과노출을 복구하지는
않습니다.

## InfluxDB 2.x (선택)

`config.json`에서 `influx.enabled`를 `true`로 바꾸고 토큰을 환경 변수로
전달합니다. 토큰은 설정 파일에 저장하지 않습니다.

```bash
export INFLUXDB_TOKEN='...'
python3 -m brio_ocr_monitor run --config config.json
```

기록 필드는 `status`, `raw_text`, `value`(검증된 값이 숫자인 경우)입니다.

## systemd 사용자 서비스 (선택)

[`systemd/brio-ocr.service`](systemd/brio-ocr.service)의 경로와 사용자명을 실제
설치 환경에 맞게 수정한 뒤 사용자 서비스로 등록할 수 있습니다. USB 연결이
불안정한 동안에는 서비스 자동 시작보다 터미널에서 `loop`를 관찰하는 편이
문제 분리에 유리합니다.
