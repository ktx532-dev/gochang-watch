"""
고창군 시설예약 빈방 체크 (GitHub Actions용, 1회 실행)

설정은 GitHub 저장소 Variables에서 (코드 수정 불필요):
  WATCH_DATES   감시할 날짜, 쉼표 구분       예) 2026-11-13,2026-11-14,2026-12-05
  ACTIVE_HOURS  감시할 시간대(한국시간)      예) 07-24  /  09-18  /  비우면 24시간
  FACILITIES    감시할 시설 목록 (비우면 아래 DEFAULT_FACILITIES)
                형식: 이름:menuCd:roomType, 쉼표 구분
  TEST_ALERT    1 이면 실행할 때마다 현재 상태를 알림으로 보냄 (테스트용)
Secret:
  NTFY_TOPIC    ntfy 구독 주제
"""
import os, json, datetime
from collections import defaultdict
import requests
from bs4 import BeautifulSoup

KST = datetime.timezone(datetime.timedelta(hours=9))
DEFAULT_FACILITIES = ("수련관 기숙사:DOM_000002402003001000:1001,"
                      "선운산유스호스텔:DOM_000002402002000000:1001")
NTFY_TOPIC = os.environ["NTFY_TOPIC"]
STATE_FILE = "state.json"
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/130 Safari/537.36"}
FAIL_ALERT_AT = (1, 3)   # 연속 실패 1회, 3회째에 알림


class ConfigError(Exception):
    pass


def push(msg, priority="urgent", click=None):
    h = {"Priority": priority, "Tags": "bell"}
    if click:
        h["Click"] = click
    try:
        requests.post(f"https://ntfy.sh/{NTFY_TOPIC}", data=msg.encode("utf-8"),
                      headers=h, timeout=10).raise_for_status()
        print("PUSH:", msg)
    except Exception as e:
        print("PUSH 실패:", e, "|", msg)


def parse_facilities():
    raw = (os.getenv("FACILITIES") or DEFAULT_FACILITIES).strip()
    out = []
    for item in filter(None, (x.strip() for x in raw.split(","))):
        parts = [p.strip() for p in item.split(":")]
        if len(parts) != 3 or not all(parts):
            raise ConfigError(f"FACILITIES 형식 오류: '{item}' (이름:menuCd:roomType)")
        out.append(tuple(parts))
    return out


def in_active_hours(now):
    spec = (os.getenv("ACTIVE_HOURS") or "").strip()
    if not spec:
        return True
    try:
        start, end = (int(x) for x in spec.split("-"))
    except ValueError:
        raise ConfigError(f"ACTIVE_HOURS 형식 오류: '{spec}' (예: 07-24)")
    h = now.hour
    return start <= h < end if start < end else (h >= start or h < end)


def parse_dates(today):
    raw = (os.getenv("WATCH_DATES") or "").replace(" ", "")
    dates = []
    for s in filter(None, raw.split(",")):
        try:
            y, m, d = (int(x) for x in s.split("-"))   # 2026-11-5 같은 표기도 허용
            dt = datetime.date(y, m, d)
        except ValueError:
            raise ConfigError(f"WATCH_DATES 형식 오류: '{s}' (예: 2026-11-13)")
        if dt >= today:
            dates.append(dt)
    return dates


def month_url(menu, room, month):
    return (f"https://www.gochang.go.kr/reserve/index.gochang"
            f"?resvSche={month}&menuCd={menu}&roomType={room}")


def find_cell(soup, day):
    for table in soup.find_all("table"):
        if "일" not in table.get_text()[:50]:
            continue
        for a in table.find_all("a"):
            if a.get_text(strip=True) == str(day):
                td = a.find_parent("td")
                return (str(td) if td else str(a)), a.get("href", "")
    return None, ""


def main():
    now = datetime.datetime.now(KST)
    test = (os.getenv("TEST_ALERT") or "").strip() == "1"

    try:
        facilities = parse_facilities()
        if not test and not in_active_hours(now):
            print(f"{now:%H:%M} 감시 시간대 아님 → 건너뜀")
            return
        dates = parse_dates(now.date())
    except ConfigError as e:
        push(f"⚠️ 고창 감시 설정 오류\n{e}", priority="high")
        raise SystemExit(1)

    if not dates:
        print("감시할 날짜 없음 (WATCH_DATES 확인)")
        return

    state = {}
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, encoding="utf-8") as f:
            state = json.load(f)
    cells = state.setdefault("cells", {})
    fails = state.get("fails")
    if not isinstance(fails, dict):          # 구버전 state 호환
        fails = {}
    state["fails"] = fails
    broken = state.setdefault("broken", {})

    by_month = defaultdict(list)
    for d in dates:
        by_month[d.strftime("%Y-%m")].append(d)

    keep, status = set(), []
    for name, menu, room in facilities:
        fac = f"{menu}:{room}"
        for month, ds in by_month.items():
            keys = {d: f"{fac}:{d.isoformat()}" for d in ds}
            keep.update(keys.values())         # 접속 실패해도 기존 상태 유지
            url = month_url(menu, room, month)

            try:
                r = requests.get(url, headers=HEADERS, timeout=20)
                r.raise_for_status()
            except Exception as e:
                fails[fac] = fails.get(fac, 0) + 1
                print(f"[{name}] 접속 실패({fails[fac]}):", e)
                if test or fails[fac] in FAIL_ALERT_AT:
                    push(f"⚠️ [{name}] 사이트 접속 실패 ({fails[fac]}회 연속)", priority="default")
                for d in ds:
                    status.append(f"[{name}] {d.month}/{d.day}: ❓ 접속 실패")
                continue

            if fails.get(fac, 0) >= FAIL_ALERT_AT[0]:
                push(f"✅ [{name}] 사이트 접속 복구", priority="low")
            fails[fac] = 0

            soup = BeautifulSoup(r.text, "html.parser")
            for d in ds:
                key = keys[d]
                label = f"[{name}] {d.month}/{d.day}"
                raw, href = find_cell(soup, d.day)

                if raw is None:                 # 달력 구조가 바뀌었거나 이상한 페이지
                    if not broken.get(key):
                        push(f"⚠️ {label} 달력에서 날짜를 못 찾음. 사이트 구조 변경 가능성, 직접 확인 필요",
                             priority="high", click=url)
                        broken[key] = True
                    status.append(f"{label}: ❓ 판정 불가")
                    continue
                broken.pop(key, None)

                avail = f"searchDay={d.day:02d}" in href or f"searchDay={d.day}&" in href
                prev = cells.get(key)
                day_url = f"{url}&searchDay={d.day:02d}"
                print(f"{label}: {'예약가능' if avail else '불가'}  href={href}")

                if avail and (prev is None or not prev["avail"]):
                    push(f"🔥 {label} 예약 가능!", click=day_url)
                elif prev is None:
                    push(f"✅ 고창 감시 시작 {label}: 현재 불가", priority="default", click=url)
                elif not avail and raw != prev["raw"]:
                    push(f"{label} 칸 상태 변경 (확인 필요)", priority="high", click=url)

                status.append(f"{label}: {'🟢 예약가능' if avail else '⚪ 불가'}")
                cells[key] = {"raw": raw, "avail": avail}

    for k in list(cells):
        if k not in keep:
            del cells[k]
    for k in list(broken):
        if k not in keep:
            del broken[k]

    if test:
        push(f"🧪 테스트 알림 ({now:%m/%d %H:%M})\n" + ("\n".join(status) or "조회 결과 없음"),
             priority="default")

    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
