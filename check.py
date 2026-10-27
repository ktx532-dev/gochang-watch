"""
고창군 청소년수련관 기숙사 빈방 체크 (GitHub Actions용, 1회 실행)

설정은 GitHub 저장소 Variables에서 (코드 수정 불필요):
  WATCH_DATES   감시할 날짜, 쉼표 구분       예) 2026-11-13,2026-11-14,2026-12-05
  TEST_ALERT    1 이면 실행할 때마다 현재 상태를 알림으로 보냄 (테스트용, 끝나면 삭제)
  ACTIVE_HOURS  감시할 시간대(한국시간)      예) 07-24  /  09-18  /  비우면 24시간
  FACILITIES    감시할 시설 목록 (비우면 아래 DEFAULT_FACILITIES)
                형식: 이름:menuCd:roomType, 쉼표 구분
                예) 수련관:DOM_000002402003001000:1001,선운산:DOM_000002402002000000:1001
Secret:
  NTFY_TOPIC    ntfy 구독 주제
"""
import os, json, sys, datetime
from collections import defaultdict
import requests
from bs4 import BeautifulSoup

KST = datetime.timezone(datetime.timedelta(hours=9))
DEFAULT_FACILITIES = ("수련관 기숙사:DOM_000002402003001000:1001,"
                      "선운산유스호스텔:DOM_000002402002000000:1001")
NTFY_TOPIC = os.environ["NTFY_TOPIC"]
STATE_FILE = "state.json"
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/130 Safari/537.36"}


def parse_facilities():
    raw = (os.getenv("FACILITIES") or DEFAULT_FACILITIES).strip()
    out = []
    for item in filter(None, (x.strip() for x in raw.split(","))):
        name, menu, room = item.split(":")
        out.append((name.strip(), menu.strip(), room.strip()))
    return out


def month_url(menu, room, month):
    return (f"https://www.gochang.go.kr/reserve/index.gochang"
            f"?resvSche={month}&menuCd={menu}&roomType={room}")


def push(msg, priority="urgent", click=None):
    h = {"Priority": priority, "Tags": "bell"}
    if click:
        h["Click"] = click
    requests.post(f"https://ntfy.sh/{NTFY_TOPIC}", data=msg.encode("utf-8"), headers=h, timeout=10)
    print("PUSH:", msg)


def in_active_hours(now):
    spec = (os.getenv("ACTIVE_HOURS") or "").strip()
    if not spec:
        return True
    start, end = (int(x) for x in spec.split("-"))
    h = now.hour
    return start <= h < end if start < end else (h >= start or h < end)  # 22-06 같은 야간도 지원


def parse_dates(today):
    raw = (os.getenv("WATCH_DATES") or "").replace(" ", "")
    dates = []
    for s in filter(None, raw.split(",")):
        d = datetime.date.fromisoformat(s)
        if d >= today:                     # 지난 날짜는 자동 제외
            dates.append(d)
    return dates


def find_cell(soup, day):
    for table in soup.find_all("table"):
        if "일" not in table.get_text()[:50]:
            continue
        for a in table.find_all("a"):
            if a.get_text(strip=True) == str(day):
                td = a.find_parent("td")
                return (str(td) if td else str(a)), a.get("href", "")
    return "NOT_FOUND", ""


def main():
    now = datetime.datetime.now(KST)
    test = (os.getenv("TEST_ALERT") or "").strip() == "1"
    status = []
    if not test and not in_active_hours(now):
        print(f"{now:%H:%M} 감시 시간대 아님 → 건너뜀")
        return

    dates = parse_dates(now.date())
    if not dates:
        print("감시할 날짜 없음 (WATCH_DATES 확인)")
        return

    state = json.load(open(STATE_FILE, encoding="utf-8")) if os.path.exists(STATE_FILE) else {}
    cells = state.setdefault("cells", {})

    by_month = defaultdict(list)
    for d in dates:
        by_month[d.strftime("%Y-%m")].append(d)

    keep = set()
    for name, menu, room in parse_facilities():
      for month, ds in by_month.items():
        url = month_url(menu, room, month)
        try:
            r = requests.get(url, headers=HEADERS, timeout=20)
            r.raise_for_status()
            state["fails"] = 0
        except Exception as e:
            state["fails"] = state.get("fails", 0) + 1
            print("접속 실패:", e)
            if state["fails"] in (1, 3):
                push(f"고창 감시({name}): 사이트 접속 실패 ({state['fails']}회 연속)\n{e}", priority="default")
            continue

        soup = BeautifulSoup(r.text, "html.parser")
        for d in ds:
            key = f"{menu}:{room}:{d.isoformat()}"
            keep.add(key)
            raw, href = find_cell(soup, d.day)
            avail = f"searchDay={d.day:02d}" in href or f"searchDay={d.day}&" in href
            prev = cells.get(key)
            label = f"[{name}] {d.month}/{d.day}"
            print(f"{label}: {'예약가능' if avail else '불가'}  href={href}")

            if prev is None:
                push(f"고창 감시 시작 ✅ {label}: {'예약가능' if avail else '불가'}",
                     priority="default", click=url)
            elif avail and not prev["avail"]:
                push(f"🔥 {label} 예약 열림!", click=f"{url}&searchDay={d.day:02d}")
            elif not avail and raw != prev["raw"]:
                push(f"{label} 칸 상태 변경 (확인 필요)", priority="high", click=url)

            status.append(f"{label}: {'🟢 예약가능' if avail else '⚪ 불가'}")
            cells[key] = {"raw": raw, "avail": avail}

    # 감시 목록에서 빠진 날짜는 state에서 정리
    for k in list(cells):
        if k not in keep:
            del cells[k]

    if test:
        push(f"🧪 테스트 알림 ({now:%m/%d %H:%M})\n" + ("\n".join(status) or "조회 결과 없음"),
             priority="default")

    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
