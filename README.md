# BRIO 500 화면 OCR 모니터

Logitech BRIO 500으로 모니터/장비 화면을 주기적으로 촬영하고, 지정 영역(ROI)을
전처리한 뒤 Tesseract OCR 결과를 CSV 또는 InfluxDB에 기록하는 작은 Linux용
파이프라인입니다.

## 먼저 확인할 것

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
./scripts/web-service.sh
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
PORT=8090 ./scripts/web-service.sh
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
