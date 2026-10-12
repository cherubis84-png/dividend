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
MONTHS = 60  # 배당률 비교용 월말 주가 기간(5년)

DART = "https://opendart.fss.or.kr/api"
PRICE_URL = ("https://apis.data.go.kr/1160100/"
             "GetStockSecuritiesInfoService_V2/getStockPriceInfo")

S = requests.Session()
S.headers["User-Agent"] = "Mozilla/5.0 (dividend-scanner)"
TODAY = dt.date.today()
# 사업보고서는 3월 말까지 제출 → 4월부터 직전 연도 배당이 확정 자료로 잡힘
LATEST_FY = TODAY.year - 1 if TODAY.month >= 4 else TODAY.year - 2


def log(*a):
    print(*a, flush=True)


def get_json(url, params, tries=4):
    last_err = None
    for i in range(tries):
        try:
            r = S.get(url, params=params, timeout=40)
            r.raise_for_status()
            try:
                return r.json()
            except ValueError:
                raise RuntimeError("JSON 아님(키 오류 가능): " + r.text[:200])
        except Exception as e:  # noqa: BLE001
            last_err = e
            log("  재시도", url.rsplit("/", 1)[-1], e)
            time.sleep(3 * (i + 1))
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
            "basDt": day.strftime("%Y%m%d"), "numOfRows": 4000, "pageNo": page})
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
    """연도별 보통주 주당현금배당금(원), 현금배당성향(%)  — 사업보고서 3년치 × 4회 = 12년"""
    dps, pay_c, pay_s = {}, {}, {}
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
                if "주당현금배당금" in se and ("보통" in kind or kind == ""):
                    if yr not in dps and raw is not None:
                        v = num(raw)
                        dps[yr] = v if v is not None else 0.0  # '-' 는 무배당
                elif "현금배당성향" in se:
                    v = num(raw)
                    tgt = pay_c if "연결" in se else pay_s
                    if yr not in tgt and v is not None:
                        tgt[yr] = v
    payout = {**pay_s, **pay_c}  # 연결 우선
    return dps, payout


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


def analyze(code, row, hist, dps, payout, fs, cyc=1, ps=(None, None)):
    price = num(row.get("clpr"))
    last = dps.get(LATEST_FY)
    if not price or not last or last <= 0:
        return None

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
    pct = (sum(1 for v in vals if v < cur_y) / len(vals) * 100) if vals else None

    prices = [p for _, p, _ in hist if p] + [price]
    dd52 = price / max(prices[-13:]) - 1
    dd5y = price / max(prices) - 1

    # 주식분할·병합 의심(상장주식수 급변) → 배당·주가 왜곡 경고
    shares = [s for _, _, s in hist if s] + [num(row.get("lstgStCnt")) or 0]
    split = any(b and a and (b / a > 1.5 or b / a < 0.67) for a, b in zip(shares, shares[1:]))

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
    shares_now = num(row.get("lstgStCnt"))
    eps, eq_parent = ps
    ni0 = acct(fs, "당기순이익")[0]
    if eps is None and shares_now and ni0 is not None:
        eps = ni0 / shares_now
    if eq_parent is None:
        eq_parent = eq
    per = price / eps if eps and eps > 0 else None
    bps = eq_parent / shares_now if eq_parent and shares_now else None
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

    disc = clamp(((ratio or 1) - 1) / 0.5) * 20 + clamp(-dd52 / 0.4) * 10

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
        "split": split,
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

    cur_day, cur = snapshot_on_or_before(TODAY)
    if not cur:
        raise SystemExit("최근 시세를 받지 못했습니다 (공공데이터포털 키·활용신청 확인)")
    log("시세 기준일", cur_day, "종목", len(cur))

    cmap = corp_codes()
    uni = {}
    for code, r in cur.items():
        if r.get("mrktCtg") not in ("KOSPI", "KOSDAQ"):
            continue
        if not code.endswith("0") or "스팩" in (r.get("itmsNm") or ""):
            continue  # 우선주·스팩 제외
        if (num(r.get("mrktTotAmt")) or 0) < MIN_CAP_EOK * 1e8:
            continue
        if code in cmap:
            uni[code] = r
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

    divs = {}
    for i, c in enumerate(uni, 1):
        try:
            divs[c] = dividends(cmap[c])
        except Exception as e:  # noqa: BLE001
            log("  배당 실패", c, e)
        if i % 100 == 0:
            log(f"  배당 {i}/{len(uni)}")

    payers = [c for c in uni if divs.get(c) and (divs[c][0].get(LATEST_FY) or 0) > 0]
    fins = financials([cmap[c] for c in payers], LATEST_FY)
    log("배당 지급 종목", len(payers))

    extra = {}
    for i, c in enumerate(payers, 1):
        try:
            extra[c] = (pay_cycle(cmap[c]), per_share(cmap[c], LATEST_FY))
        except Exception as e:  # noqa: BLE001
            log("  주기·EPS 실패", c, e)
        if i % 100 == 0:
            log(f"  주기·EPS {i}/{len(payers)}")

    out = []
    for c in payers:
        dps, payout = divs[c]
        cyc, ps = extra.get(c, (1, (None, None)))
        a = analyze(c, uni[c], hist[c], dps, payout, fins.get(cmap[c], {}), cyc, ps)
        if a:
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
