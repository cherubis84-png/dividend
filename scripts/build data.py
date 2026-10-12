#!/usr/bin/env python3
"""
국내 배당성장 + 일시적 고배당 종목 데이터 생성기

- 시세·시총·상장주식수 : 공공데이터포털 '금융위원회_주식시세정보' (getStockPriceInfo)
- 배당(주당배당금·배당성향), 재무(부채·유동성·이익) : DART OpenAPI
결과 : 저장소 루트의 data.json  (index.html 이 읽어서 화면에 표시)

환경변수
  DART_API_KEY    : DART 인증키
  DATA_GO_KR_KEY  : 공공데이터포털 '일반 인증키(Decoding)'
  MIN_CAP_EOK     : 대상 시가총액 하한(억원, 기본 2000)
"""
import io
import os
import json
import time
import zipfile
import statistics
import datetime as dt
import xml.etree.ElementTree as ET

import requests

DART_KEY = os.environ.get("DART_API_KEY", "")
PRICE_KEY = os.environ.get("DATA_GO_KR_KEY", "")
MIN_CAP_EOK = float(os.environ.get("MIN_CAP_EOK", "2000"))
MIN_CAP_PREF_EOK = float(os.environ.get("MIN_CAP_PREF_EOK", "1000"))  # 우선주 시총 하한
MONTHS = 60  # 배당률 비교용 월말 주가 기간(5년)

DART = "https://opendart.fss.or.kr/api"
PRICE_BASE = "https://apis.data.go.kr/1160100/"
PRICE_CANDIDATES = [  # 포털 버전에 따라 주소가 다름 → 응답하는 것을 사용
    "GetStockSecuritiesInfoService_V2/getStockPriceInfo_V2",
    "GetStockSecuritiesInfoService_V2/getStockPriceInfo",
    "GetStockSecuritiesInfoService/getStockPriceInfo",
    "service/GetStockSecuritiesInfoService/getStockPriceInfo",
]
PRICE_URL = PRICE_BASE + PRICE_CANDIDATES[0]

S = requests.Session()
S.headers["User-Agent"] = "Mozilla/5.0 (dividend-scanner)"
TODAY = dt.date.today()
# 사업보고서는 3월 말까지 제출 → 4월부터 직전 연도 배당이 확정 자료로 잡힘
LATEST_FY = TODAY.year - 1 if TODAY.month >= 4 else TODAY.year - 2


def log(*a):
    print(*a, flush=True)


class ApiError(Exception):
    pass


def get_json(url, params, tries=4):
    last_err = None
    for i in range(tries):
        try:
            r = S.get(url, params=params, timeout=40)
        except Exception as e:  # noqa: BLE001  (네트워크 오류만 재시도)
            last_err = e
            log("  재시도", url.rsplit("/", 1)[-1], e)
            time.sleep(3 * (i + 1))
            continue
        if r.status_code == 429 or r.status_code >= 500:
            last_err = ApiError(f"HTTP {r.status_code}: {r.text[:300]}")
            log("  재시도", url.rsplit("/", 1)[-1], last_err)
            time.sleep(3 * (i + 1))
            continue
        if r.status_code != 200:
            raise ApiError(f"HTTP {r.status_code} ({url.rsplit('/', 1)[-1]}) 서버 응답: {r.text[:500]}")
        try:
            return r.json()
        except ValueError:
            raise ApiError("JSON 이 아닌 응답(인증키 오류 가능): " + r.text[:500])
    raise last_err


def num(v):
    if v is None:
        return None
    s = str(v).replace(",", "").strip()
    if s in ("", "-"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


# ───────────────────────── 시세 ─────────────────────────
def price_snapshot(day):
    """해당 일자 전 종목 시세 {단축코드: row}. 휴장일·미반영이면 {}"""
    rows, page = [], 1
    while True:
        j = get_json(PRICE_URL, {
            "serviceKey": PRICE_KEY, "resultType": "json",
            "basDt": day.strftime("%Y%m%d"), "numOfRows": 1000, "pageNo": page})
        head = j.get("response", {}).get("header", {})
        if head and head.get("resultCode") not in (None, "00"):
            raise ApiError(f"시세 API 오류 {head.get('resultCode')}: {head.get('resultMsg')}")
        body = j.get("response", {}).get("body", {})
        items = body.get("items") or {}
        it = items.get("item", []) if isinstance(items, dict) else []
        if isinstance(it, dict):
            it = [it]
        rows += it
        if not it or len(rows) >= int(body.get("totalCount", 0) or 0):
            break
        page += 1
    return {r["srtnCd"]: r for r in rows if r.get("srtnCd")}


def snapshot_on_or_before(day, max_back=12):
    for k in range(max_back):
        d = day - dt.timedelta(days=k)
        if d.weekday() >= 5:
            continue
        snap = price_snapshot(d)
        if snap:
            return d, snap
    return None, {}


def month_ends(n):
    """이번 달을 제외한 직전 n개월의 말일 (오래된 순)"""
    y, m, out = TODAY.year, TODAY.month, []
    for _ in range(n):
        m -= 1
        if m == 0:
            y, m = y - 1, 12
        first_next = dt.date(y + (m == 12), m % 12 + 1, 1)
        out.append(first_next - dt.timedelta(days=1))
    return list(reversed(out))


# ───────────────────────── DART ─────────────────────────
def corp_codes():
    r = S.get(f"{DART}/corpCode.xml", params={"crtfc_key": DART_KEY}, timeout=90)
    r.raise_for_status()
    z = zipfile.ZipFile(io.BytesIO(r.content))
    root = ET.fromstring(z.read(z.namelist()[0]))
    m = {}
    for el in root.iter("list"):
        sc = (el.findtext("stock_code") or "").strip()
        if sc:
            m[sc] = el.findtext("corp_code")
    return m


def dividends(corp):
    """연도별 주당현금배당금(원) {'보통': {...}, '우선': {...}}, 현금배당성향(%)
    — 사업보고서 3년치 × 4회 = 12년"""
    dps_by, pay_c, pay_s = {"보통": {}, "우선": {}}, {}, {}
    for y in (LATEST_FY, LATEST_FY - 3, LATEST_FY - 6, LATEST_FY - 9):
        j = get_json(f"{DART}/alotMatter.json", {
            "crtfc_key": DART_KEY, "corp_code": corp,
            "bsns_year": str(y), "reprt_code": "11011"})
        time.sleep(0.06)
        if j.get("status") != "000":
            continue
        for it in j.get("list", []):
            se = (it.get("se") or "").replace(" ", "")
            kind = it.get("stock_knd") or ""
            for off, key in ((0, "thstrm"), (1, "frmtrm"), (2, "lwfr")):
                yr = y - off
                raw = it.get(key)
                if "주당현금배당금" in se:
                    k = "우선" if "우선" in kind else "보통" if ("보통" in kind or kind == "") else None
                    dps = dps_by.get(k)
                    if dps is not None and yr not in dps and raw is not None:
                        v = num(raw)
                        dps[yr] = v if v is not None else 0.0  # '-' 는 무배당
                elif "현금배당성향" in se:
                    v = num(raw)
                    tgt = pay_c if "연결" in se else pay_s
                    if yr not in tgt and v is not None:
                        tgt[yr] = v
    payout = {**pay_s, **pay_c}  # 연결 우선
    return dps_by, payout


# 한국표준산업분류(KSIC) 앞자리 → 섹터 이름 (가장 긴 접두어 우선)
KSIC = {
    "10": "식품", "11": "음료·주류", "12": "담배", "13": "섬유", "14": "의류", "15": "신발·가죽",
    "16": "목재", "17": "제지", "18": "인쇄", "19": "정유", "20": "화학", "204": "화장품·생활용품",
    "21": "제약·바이오", "22": "고무·플라스틱", "23": "시멘트·유리", "24": "철강·금속",
    "25": "금속가공", "26": "전자", "261": "반도체", "262": "전자부품", "263": "컴퓨터",
    "264": "통신장비", "265": "가전·영상기기", "27": "의료·정밀기기", "28": "전기장비",
    "282": "2차전지", "29": "기계", "30": "자동차", "303": "자동차부품", "31": "조선·운송장비",
    "311": "조선", "313": "항공우주·방산", "32": "가구", "33": "기타제조", "35": "전력·가스",
    "36": "수도", "38": "환경", "41": "건설", "42": "건설", "45": "자동차판매", "46": "도매·상사",
    "47": "유통", "49": "육상운송", "50": "해운", "51": "항공", "52": "물류", "55": "숙박·레저",
    "56": "외식", "58": "출판", "582": "소프트웨어", "59": "미디어·엔터", "60": "방송",
    "61": "통신", "62": "IT서비스", "63": "인터넷·플랫폼", "64": "금융", "641": "은행",
    "6499": "지주회사", "64992": "지주회사", "65": "보험", "66": "금융서비스", "6612": "증권",
    "68": "부동산", "70": "연구개발", "71": "전문서비스", "72": "엔지니어링", "73": "광고",
    "74": "사업지원", "75": "사업지원", "85": "교육", "86": "의료", "90": "문화·여가",
    "91": "스포츠·레저",
}


def sector_of(corp):
    j = get_json(f"{DART}/company.json", {"crtfc_key": DART_KEY, "corp_code": corp})
    time.sleep(0.06)
    code = (j.get("induty_code") or "").strip()
    for n in range(len(code), 1, -1):
        if code[:n] in KSIC:
            return KSIC[code[:n]], code
    return "기타", code


def interim_dps(corp, year, reprt):
    """분기·반기보고서의 당기 주당현금배당금(보통주). 보고서 없으면 None"""
    j = get_json(f"{DART}/alotMatter.json", {
        "crtfc_key": DART_KEY, "corp_code": corp,
        "bsns_year": str(year), "reprt_code": reprt})
    time.sleep(0.06)
    if j.get("status") != "000":
        return None
    for it in j.get("list", []):
        se = (it.get("se") or "").replace(" ", "")
        kind = it.get("stock_knd") or ""
        if "주당현금배당금" in se and ("보통" in kind or kind == ""):
            return num(it.get("thstrm")) or 0.0
    return 0.0


def pay_cycle(corp):
    """배당 주기: 4=분기, 2=반기, 1=연 1회.
    1분기보고서에 배당이 있으면 분기, 반기보고서에만 있으면 반기(중간)배당"""
    for y in (TODAY.year, LATEST_FY):
        h = interim_dps(corp, y, "11012")
        if h is None:
            continue  # 해당 연도 반기보고서 미제출 → 직전 연도로
        q1 = interim_dps(corp, y, "11013")
        return 4 if (q1 or 0) > 0 else 2 if h > 0 else 1
    return 1


def per_share(corp, year):
    """사업보고서 전체재무제표에서 기본주당이익(EPS)·지배주주지분 (연결 우선)"""
    for fs_div in ("CFS", "OFS"):
        j = get_json(f"{DART}/fnlttSinglAcntAll.json", {
            "crtfc_key": DART_KEY, "corp_code": corp, "bsns_year": str(year),
            "reprt_code": "11011", "fs_div": fs_div})
        time.sleep(0.06)
        if j.get("status") != "000":
            continue
        eps = eq_parent = None
        for it in j.get("list", []):
            aid = it.get("account_id") or ""
            nm = (it.get("account_nm") or "").replace(" ", "")
            v = num(it.get("thstrm_amount"))
            if v is None:
                continue
            if eps is None and it.get("sj_div") in ("IS", "CIS") and (
                    aid == "ifrs-full_BasicEarningsLossPerShare"
                    or (nm.startswith("기본주당") and "계속" not in nm)):
                eps = v
            if eq_parent is None and it.get("sj_div") == "BS" and (
                    aid == "ifrs-full_EquityAttributableToOwnersOfParent"
                    or "지배기업" in nm and "소유주" in nm and "자본" in nm):
                eq_parent = v
        return eps, eq_parent
    return None, None


def financials(corps, year):
    """주요계정(연결 우선) {corp: {계정명: [당기, 전기, 전전기]}}"""
    raw = {}
    for i in range(0, len(corps), 100):
        chunk = corps[i:i + 100]
        j = get_json(f"{DART}/fnlttMultiAcnt.json", {
            "crtfc_key": DART_KEY, "corp_code": ",".join(chunk),
            "bsns_year": str(year), "reprt_code": "11011"})
        time.sleep(0.1)
        if j.get("status") != "000":
            continue
        for it in j.get("list", []):
            d = raw.setdefault(it["corp_code"], {}).setdefault(it.get("fs_div"), {})
            nm = (it.get("account_nm") or "").replace(" ", "")
            d.setdefault(nm, [num(it.get("thstrm_amount")),
                              num(it.get("frmtrm_amount")),
                              num(it.get("bfefrmtrm_amount"))])
    return {c: (v.get("CFS") or v.get("OFS") or {}) for c, v in raw.items()}


def acct(fs, prefix):
    for k, v in fs.items():
        if k.startswith(prefix):
            return v
    return [None, None, None]


# ───────────────────────── 분석 ─────────────────────────
def fy_of(ym):
    """해당 월에 시장이 알고 있던 최신 확정 배당 연도"""
    y, m = int(ym[:4]), int(ym[4:])
    return y - 1 if m >= 4 else y - 2


def clamp(x, lo=0.0, hi=1.0):
    return max(lo, min(hi, x))


def analyze(code, row, hist, dps, payout, fs, cyc=1, ps=(None, None),
            common_shares=None, total_shares=None):
    price = num(row.get("clpr"))
    if not price or not (dps.get(LATEST_FY) or 0) > 0:
        return None

    # ── 주식분할·병합 보정: 상장주식수가 한 달에 1.5배 이상 변하면 분할/병합으로 보고
    #    과거 주가·배당금을 현재 주식수 기준으로 환산
    cur_sh = num(row.get("lstgStCnt"))
    seq = [(ym, p, sh) for ym, p, sh in hist] + [("now", price, cur_sh)]
    mult = [1.0] * len(seq)
    split_events = []
    for i in range(len(seq) - 2, -1, -1):
        a, b = seq[i][2], seq[i + 1][2]
        r = b / a if a and b else 1.0
        jump = r > 1.5 or r < 0.67
        if jump:
            split_events.append(seq[i + 1][0])
        mult[i] = mult[i + 1] * (r if jump else 1.0)
    split = bool(split_events)
    hist = [(ym, (p / mult[i]) if p else p, sh) for i, (ym, p, sh) in enumerate(seq[:-1])]
    if split:
        idx = {ym: i for i, (ym, _, _) in enumerate(seq[:-1])}

        def mult_for_year(y):  # 결산(12월) 시점의 환산 배수
            i = idx.get(f"{y}12")
            if i is None:
                i = 0 if f"{y}12" < seq[0][0] else len(seq) - 1
            return mult[i]
        dps = {y: v / mult_for_year(y) for y, v in dps.items()}

    last = dps[LATEST_FY]

    # 배당 지급 기간(연속 지급 연수) — 조회 가능한 기간 끝까지 이어지면 '+' 표시
    paid, y = 0, LATEST_FY
    while (dps.get(y) or 0) > 0:
        paid += 1
        y -= 1
    paid_plus = y < min(dps)
    prev = dps.get(LATEST_FY - 1) or 0
    g1 = (last / prev - 1) if prev > 0 else None

    # 배당 이력
    nocut, y = 0, LATEST_FY
    while (y - 1) in dps and dps[y - 1] > 0 and dps[y] >= dps[y - 1]:
        nocut += 1
        y -= 1
    win = range(LATEST_FY - 9, LATEST_FY + 1)
    ups = sum(1 for y in win if y in dps and y - 1 in dps and dps[y] > dps[y - 1])
    cuts = sum(1 for y in win if y in dps and y - 1 in dps and dps[y] < dps[y - 1])
    base5 = dps.get(LATEST_FY - 5) or 0
    cagr5 = (last / base5) ** (1 / 5) - 1 if base5 > 0 else None

    # 배당률 이력 (월말 종가 기준)
    yh = []
    for ym, p, _ in hist:
        d = dps.get(fy_of(ym))
        if p and d is not None:
            yh.append([ym, round(d / p * 100, 2)])
    cur_y = last / price * 100
    vals = [v for _, v in yh]
    avg5 = statistics.mean(vals) if len(vals) >= 24 else None
    ratio = cur_y / avg5 if avg5 else None
    # 배당 급증(특별배당 등) 의심: 직전 3년 평균의 1.8배 초과
    prior = [dps[y] for y in (LATEST_FY - 1, LATEST_FY - 2, LATEST_FY - 3) if y in dps]
    prior_avg = statistics.mean(prior) if prior else 0
    jump = prior_avg > 0 and last > prior_avg * 1.8
    # 할인 점수용 배당률은 급증분을 직전 3년 평균의 1.5배로 눌러서 계산 → '주가가 싸진 정도'를 반영
    d_disc = min(last, prior_avg * 1.5) if jump else last
    ratio_disc = (d_disc / price * 100) / avg5 if avg5 else None
    pct = (sum(1 for v in vals if v < cur_y) / len(vals) * 100) if vals else None

    prices = [p for _, p, _ in hist if p] + [price]
    dd52 = price / max(prices[-13:]) - 1
    dd5y = price / max(prices) - 1


    # 재무 안정성
    debt, eq = acct(fs, "부채총계")[0], acct(fs, "자본총계")[0]
    ca, cl = acct(fs, "유동자산")[0], acct(fs, "유동부채")[0]
    is_fin = ca is None and bool(fs)  # 은행·보험·증권 등은 유동성 구분이 없음
    debt_ratio = debt / eq * 100 if debt is not None and eq and eq > 0 else None
    cur_ratio = ca / cl * 100 if ca and cl else None
    op = [v for v in acct(fs, "영업이익") if v is not None]
    ni = [v for v in acct(fs, "당기순이익") if v is not None]
    op_pos = sum(1 for v in op if v > 0)
    ni_pos = sum(1 for v in ni if v > 0)
    pay = payout.get(LATEST_FY)

    # EPS·PER·PBR (사업보고서 기준. 없으면 순이익·자본총계 / 상장주식수로 근사)
    # 우선주는 회사(보통주) EPS·BPS 를 그대로 사용, BPS 는 보통+우선 합산 주식수 기준
    common_shares = common_shares or num(row.get("lstgStCnt"))
    total_shares = total_shares or common_shares
    eps, eq_parent = ps
    ni0 = acct(fs, "당기순이익")[0]
    if eps is None and total_shares and ni0 is not None:
        eps = ni0 / total_shares
    if eq_parent is None:
        eq_parent = eq
    per = price / eps if eps and eps > 0 else None
    bps = eq_parent / total_shares if eq_parent and total_shares else None
    pbr = price / bps if bps and bps > 0 else None

    # ── 점수 (100점) ──
    safe = 0.0
    if eq is not None and eq <= 0:
        safe = 0.0  # 자본잠식
    else:
        if is_fin:
            safe += 15 if ni_pos >= 3 else 0
        else:
            if debt_ratio is not None:
                safe += 10 if debt_ratio <= 50 else 7 if debt_ratio <= 100 else 4 if debt_ratio <= 150 else 0
            if cur_ratio is not None:
                safe += 5 if cur_ratio >= 200 else 4 if cur_ratio >= 150 else 2 if cur_ratio >= 100 else 0
        safe += 10 if op and op_pos == len(op) else 4 if op_pos >= 2 else 0
        safe += 5 if ni and ni_pos == len(ni) else 0

    grow = min(nocut, 10) * 2.0
    grow += clamp((cagr5 or 0) / 0.10) * 10
    if pay is not None:
        grow += 10 if 20 <= pay <= 60 else 7 if 0 < pay < 20 else 5 if pay <= 80 else 0

    disc = clamp(((ratio_disc or 1) - 1) / 0.5) * 20 + clamp(-dd52 / 0.4) * 10

    return {
        "c": code, "n": row.get("itmsNm"), "m": row.get("mrktCtg"),
        "cap": round((num(row.get("mrktTotAmt")) or 0) / 1e8),
        "p": price, "dps": {str(k): dps[k] for k in sorted(dps) if k >= LATEST_FY - 10},
        "y": round(cur_y, 2), "avg": round(avg5, 2) if avg5 else None,
        "ratio": round(ratio, 2) if ratio else None, "pct": round(pct) if pct is not None else None,
        "nocut": nocut, "ups": ups, "cuts": cuts,
        "cagr": round(cagr5 * 100, 1) if cagr5 is not None else None,
        "pay": round(pay, 1) if pay is not None else None,
        "dd52": round(dd52 * 100, 1), "dd5y": round(dd5y * 100, 1),
        "debt": round(debt_ratio) if debt_ratio is not None else None,
        "curr": round(cur_ratio) if cur_ratio is not None else None,
        "fin": is_fin, "op": op_pos, "opn": len(op), "ni": ni_pos, "nin": len(ni),
        "split": split, "jump": jump,
        "paid": paid, "paidp": paid_plus, "cyc": cyc,
        "g1": round(g1 * 100, 1) if g1 is not None else None,
        "eps": round(eps) if eps is not None else None,
        "per": round(per, 1) if per else None, "pbr": round(pbr, 2) if pbr else None,
        "s": round(safe + grow + disc, 1),
        "sp": [round(safe, 1), round(grow, 1), round(disc, 1)],
        "yh": yh,
    }


def main():
    if not DART_KEY or not PRICE_KEY:
        raise SystemExit("DART_API_KEY / DATA_GO_KR_KEY 를 Secrets 에 등록하세요.")

    # 시세 API 주소·키 점검 (1건만 요청)
    global PRICE_URL
    errs = []
    for cand in PRICE_CANDIDATES:
        try:
            j = get_json(PRICE_BASE + cand, {"serviceKey": PRICE_KEY, "resultType": "json",
                                             "numOfRows": 1, "pageNo": 1})
            head = j.get("response", {}).get("header", {})
            if head.get("resultCode") not in (None, "00"):
                raise ApiError(f"{head.get('resultCode')} {head.get('resultMsg')}")
            PRICE_URL = PRICE_BASE + cand
            log("시세 API 점검 OK:", cand, json.dumps(head, ensure_ascii=False))
            items = (j.get("response", {}).get("body", {}).get("items") or {}).get("item")
            if items:
                log("  응답 항목:", ", ".join((items[0] if isinstance(items, list) else items).keys()))
            break
        except ApiError as e:
            errs.append(f"{cand} → {e}")
    else:
        raise SystemExit("시세 API(공공데이터포털) 호출 실패\n" + "\n".join(errs))
    j = get_json(f"{DART}/company.json", {"crtfc_key": DART_KEY, "corp_code": "00126380"})
    if j.get("status") != "000":
        raise SystemExit(f"DART 인증키 오류 → {j.get('status')} {j.get('message')}")
    log("DART 점검 OK")

    cur_day, cur = snapshot_on_or_before(TODAY)
    if not cur:
        raise SystemExit("최근 시세를 받지 못했습니다 (공공데이터포털 키·활용신청 확인)")
    log("시세 기준일", cur_day, "종목", len(cur))

    cmap = corp_codes()
    uni, base_of = {}, {}  # base_of: 종목코드 → 보통주 코드
    for code, r in cur.items():
        if r.get("mrktCtg") not in ("KOSPI", "KOSDAQ") or "스팩" in (r.get("itmsNm") or ""):
            continue
        base = code if code.endswith("0") else code[:5] + "0"
        if base not in cmap:
            continue
        pref = base != code
        if (num(r.get("mrktTotAmt")) or 0) < (MIN_CAP_PREF_EOK if pref else MIN_CAP_EOK) * 1e8:
            continue
        uni[code], base_of[code] = r, base
    log("분석 대상", len(uni))

    hist = {c: [] for c in uni}
    for me in month_ends(MONTHS):
        d, snap = snapshot_on_or_before(me)
        if not snap:
            continue
        for c in uni:
            r = snap.get(c)
            if r:
                hist[c].append((d.strftime("%Y%m"), num(r.get("clpr")), num(r.get("lstgStCnt"))))
    log("월말 주가 수집 완료")

    corps = sorted({cmap[base_of[c]] for c in uni})
    divs = {}
    for i, corp in enumerate(corps, 1):
        try:
            divs[corp] = dividends(corp)
        except Exception as e:  # noqa: BLE001
            log("  배당 실패", corp, e)
        if i % 100 == 0:
            log(f"  배당 {i}/{len(corps)}")

    def dps_of(c):
        d = divs.get(cmap[base_of[c]])
        if not d:
            return None
        return d[0]["우선" if base_of[c] != c else "보통"]

    payers = [c for c in uni if (dps_of(c) or {}).get(LATEST_FY, 0) > 0]
    pay_corps = sorted({cmap[base_of[c]] for c in payers})
    fins = financials(pay_corps, LATEST_FY)
    log("배당 지급 종목", len(payers), "회사", len(pay_corps))

    extra = {}
    for i, corp in enumerate(pay_corps, 1):
        try:
            extra[corp] = (pay_cycle(corp), per_share(corp, LATEST_FY), sector_of(corp))
        except Exception as e:  # noqa: BLE001
            log("  주기·EPS·섹터 실패", corp, e)
        if i % 100 == 0:
            log(f"  주기·EPS·섹터 {i}/{len(pay_corps)}")

    # 보통주+우선주 합산 상장주식수 (BPS 계산용)
    tot_shares = {}
    for c, r in cur.items():
        b = c if c.endswith("0") else c[:5] + "0"
        if b in cmap:
            tot_shares[b] = tot_shares.get(b, 0) + (num(r.get("lstgStCnt")) or 0)

    out = []
    for c in payers:
        corp = cmap[base_of[c]]
        payout = divs[corp][1]
        cyc, ps, sec = extra.get(corp, (1, (None, None), ("기타", "")))
        common_shares = num((cur.get(base_of[c]) or {}).get("lstgStCnt"))
        a = analyze(c, uni[c], hist[c], dps_of(c), payout, fins.get(corp, {}), cyc, ps,
                    common_shares, tot_shares.get(base_of[c]))
        if a:
            a["sec"], a["ksic"] = sec
            a["pref"] = base_of[c] != c
            out.append(a)
    out.sort(key=lambda x: -x["s"])

    data = {
        "updated": dt.datetime.now(dt.timezone(dt.timedelta(hours=9))).strftime("%Y-%m-%d %H:%M"),
        "priceDate": cur_day.isoformat(), "fy": LATEST_FY, "minCap": MIN_CAP_EOK,
        "stocks": out,
    }
    with open("data.json", "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
    log("완료", len(out), "종목 저장")


if __name__ == "__main__":
    main()
