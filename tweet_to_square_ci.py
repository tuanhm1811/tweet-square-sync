#!/usr/bin/env python3
"""
tweet_to_square_ci.py  (BAN HO TRO ANH)
---------------------------------------
Chay MOT LAN roi thoat — danh cho GitHub Actions (cron).
Moi lan: doc tweet MOI tu X (kem anh) -> dang len Binance Square (kem anh)
-> cap nhat state/last_id.txt.

Anh: lay tu anh dinh kem trong tweet (toi da 4 anh - dung gioi han cua Square).
     Tweet co video/GIF se chi dang phan chu (Square can luong rieng cho video).
     Neu upload anh loi, tu dong dang lai chi voi chu.

Luong dang anh lay tu ma nguon CHINH THUC cua Binance (binance-skills-hub):
  1) POST /image/presignedUrl  (v2)  -> tra ve presignedUrl + fileTicket
  2) PUT anh len presignedUrl (S3)
  3) POST /image/imageStatus   (v2)  -> poll den khi status=1, lay imageUrl
  4) POST /content/add         (v1)  -> dang bai voi imageList

BAO MAT: tat ca key doc tu BIEN MOI TRUONG (GitHub Secrets). KHONG hardcode.
"""
import os
import re
import sys
import time
import requests
from pathlib import Path
from urllib.parse import urlparse
from requests_oauthlib import OAuth1

# ---------- Cau hinh ----------
USERNAME    = os.environ.get("TWITTER_USERNAME", "").lstrip("@")
EXCLUDE     = os.environ.get("EXCLUDE", "retweets,replies")
MAX_RESULTS = int(os.environ.get("MAX_RESULTS", "10"))
X_API_BASE  = os.environ.get("X_API_BASE", "https://api.twitter.com/2")  # loi thi doi sang https://api.x.com/2
STATE_FILE  = Path(os.environ.get("STATE_FILE", "state/last_id.txt"))
# Cache user_id de KHONG goi /users/by/username moi lan chay (endpoint nay
# co han muc rat thap ~100 req/24h, du chay 15 phut/lan la cham tran).
USER_ID_ENV  = os.environ.get("TWITTER_USER_ID", "").strip()
USER_ID_FILE = Path(os.environ.get("USER_ID_FILE", "state/user_id.txt"))
# Dem so lan dang loi lien tiep cua 1 tweet ("<tweet_id> <so_lan>").
FAIL_FILE   = Path(os.environ.get("FAIL_FILE", "state/fail_count.txt"))
# Tweet bi Square tu choi (ma loi noi dung) qua so lan nay -> bo qua de khong
# chan cac tweet sau mai mai.
MAX_FAILS   = int(os.environ.get("MAX_FAILS", "5"))

# Binance Square endpoints
SQ_V1 = "https://www.binance.com/bapi/composite/v1/public/pgc/openApi"
SQ_V2 = "https://www.binance.com/bapi/composite/v2/public/pgc/openApi"
POLL_INTERVAL = 3
MAX_POLL = 10

CT_MAP = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
          "gif": "image/gif", "webp": "image/webp"}

# X API tu gan link rut gon t.co o CUOI text cua tweet co anh/video (link tro ve
# anh/tweet goc tren Twitter) -> bo di cho khoi thua khi dang len Square.
# Chi bo link dinh o cuoi; link ban co tinh dan o giua cau van duoc giu.
TRAILING_TCO_RE = re.compile(r"\s*(?:https?://t\.co/\w+\s*)+$")

# Ma loi hay gap cua Square -> giai thich de hieu
SQ_ERR = {
    "220009": "Da vuot gioi han 100 bai/ngay.",
    "220014": "Da vuot gioi han upload anh trong ngay.",
    "20013":  "Noi dung qua dai/khong hop le.",
    "220003": "API key Square khong ton tai/da bi thu hoi -> tao key moi.",
}
# Loi do tai khoan/key/han muc (KHONG phai do noi dung tweet) -> khong bao gio
# bo qua tweet vi cac loi nay, cho het loi roi dang tiep.
SQ_ACCOUNT_ERRORS = {"220003", "220009", "220014"}


class XApiError(Exception):
    pass


class SquareError(RuntimeError):
    def __init__(self, msg, code=None):
        super().__init__(msg)
        self.code = code


def env(n):
    v = os.environ.get(n)
    if not v:
        sys.exit(f"[LOI] Thieu bien moi truong/secret: {n}")
    return v


if not USERNAME:
    sys.exit("[LOI] Chua dat TWITTER_USERNAME (sua trong file workflow sync.yml).")

oauth = OAuth1(env("TWITTER_API_KEY"), env("TWITTER_API_SECRET"),
               env("TWITTER_ACCESS_TOKEN"), env("TWITTER_ACCESS_SECRET"))
SQUARE_KEY = env("BINANCE_SQUARE_OPENAPI_KEY")
SQ_HEADERS = {
    "X-Square-OpenAPI-Key": SQUARE_KEY,
    "Content-Type": "application/json",
    "clienttype": "binanceSkill",
}


# ---------- State ----------
def read_state():
    try:
        return STATE_FILE.read_text().strip() or None
    except FileNotFoundError:
        return None


def write_state(v):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(str(v))


def read_fail():
    try:
        tid, n = FAIL_FILE.read_text().split()
        return tid, int(n)
    except (FileNotFoundError, ValueError):
        return None, 0


def write_fail(tid, n):
    if tid is None:
        FAIL_FILE.unlink(missing_ok=True)
    else:
        FAIL_FILE.parent.mkdir(parents=True, exist_ok=True)
        FAIL_FILE.write_text(f"{tid} {n}")


def explain_x_error(r):
    """Giai thich loi X API de biet can lam gi (thay vi im lang)."""
    hint = {
        401: "Key/token X sai hoac da bi reset -> tao lai 4 key trong X Developer Portal.",
        402: "Tai khoan X API het credit / can nap tien (goi pay-per-use).",
        403: "App X khong co quyen goi endpoint nay (goi API/Project bi doi hoac bi khoa).",
        429: "Vuot han muc X API (rate limit hoac het quota thang - UsageCapExceeded).",
    }.get(r.status_code, "")
    extra = ""
    reset = r.headers.get("x-rate-limit-reset")
    if reset:
        extra = f" | reset luc {time.strftime('%Y-%m-%d %H:%M:%SZ', time.gmtime(int(reset)))}"
    return f"X API loi {r.status_code}: {r.text[:300]} {hint}{extra}".strip()


# ---------- Twitter ----------
def get_user_id(u):
    r = requests.get(f"{X_API_BASE}/users/by/username/{u}", auth=oauth, timeout=30)
    if r.status_code != 200:
        raise XApiError(explain_x_error(r))
    return r.json()["data"]["id"]


def get_new_tweets(uid, since):
    params = {
        "max_results": MAX_RESULTS,
        # note_tweet: tweet dai > 280 ky tu, neu khong xin thi X cat cut "text".
        "tweet.fields": "created_at,attachments,note_tweet",
        "expansions": "attachments.media_keys",
        "media.fields": "url,type",
    }
    if EXCLUDE:
        params["exclude"] = EXCLUDE
    if since:
        params["since_id"] = since
    r = requests.get(f"{X_API_BASE}/users/{uid}/tweets", params=params, auth=oauth, timeout=30)
    if r.status_code != 200:
        # Truoc day chi print roi tra ve [] -> nhin log tuong "khong co tweet moi"
        # va job van xanh. Gio nem loi de job do + GitHub gui mail bao.
        raise XApiError(explain_x_error(r))
    j = r.json()
    if "data" not in j and j.get("errors"):
        raise XApiError(f"X API tra ve loi: {str(j['errors'])[:300]}")
    media_map = {m["media_key"]: m for m in j.get("includes", {}).get("media", [])}
    out = []
    for tw in j.get("data", []):
        keys = (tw.get("attachments") or {}).get("media_keys", [])
        photos, has_other = [], False
        for k in keys:
            m = media_map.get(k, {})
            if m.get("type") == "photo" and m.get("url"):
                photos.append(m["url"])
            elif m.get("type") in ("video", "animated_gif"):
                has_other = True
        tw["_photos"] = photos[:4]
        tw["_has_other_media"] = has_other
        note = (tw.get("note_tweet") or {}).get("text")
        if note:
            tw["text"] = note
        out.append(tw)
    return list(reversed(out))  # cu -> moi


def strip_trailing_tco(text):
    """Bo cac link t.co dinh o CUOI text (link anh/tweet goc do X tu gan)."""
    return TRAILING_TCO_RE.sub("", text).strip()


# ---------- Binance Square ----------
def sq_api(base, endpoint, body, timeout=60):
    r = requests.post(f"{base}{endpoint}", headers=SQ_HEADERS, json=body, timeout=timeout)
    if endpoint == "/content/add" and r.status_code == 504:
        return {"id": None, "shareLink": None}  # 504 sau khi submit = coi nhu da dang
    try:
        j = r.json()
    except Exception:
        raise RuntimeError(f"Square tra ve non-JSON ({r.status_code})")
    if j.get("code") != "000000":
        code = j.get("code")
        hint = SQ_ERR.get(code, "")
        raise SquareError(f"code={code} msg={j.get('message')} {hint}".strip(), code)
    return j.get("data")


def ext_from_url(url):
    p = urlparse(url).path
    ext = p.rsplit(".", 1)[-1].lower() if "." in p else "jpg"
    return ext if ext in CT_MAP else "jpg"


def upload_one_image(img_url):
    ext = ext_from_url(img_url)
    # 1) xin presigned url
    d = sq_api(SQ_V2, "/image/presignedUrl", {"imageName": f"image.{ext}"})
    presigned, ticket = d["presignedUrl"], d["fileTicket"]
    # 2) tai anh tu Twitter ve roi PUT len S3
    img = requests.get(img_url, timeout=60)
    img.raise_for_status()
    put = requests.put(presigned, headers={"Content-Type": CT_MAP[ext]},
                       data=img.content, timeout=120)
    if not put.ok:
        raise RuntimeError(f"Upload S3 that bai: {put.status_code}")
    # 3) poll den khi xu ly xong
    for i in range(MAX_POLL):
        s = sq_api(SQ_V2, "/image/imageStatus", {"fileTicket": ticket})
        if s.get("status") == 1:
            return s["imageUrl"]
        if s.get("status") == 2:
            raise RuntimeError(f"Xu ly anh that bai: {s.get('failedReason')}")
        time.sleep(POLL_INTERVAL)
    raise RuntimeError("Cho xu ly anh qua lau (timeout).")


def post_to_square(text, photo_urls):
    body = {"contentType": 1, "bodyTextOnly": text}
    if photo_urls:
        body["imageList"] = [upload_one_image(u) for u in photo_urls]
    data = sq_api(SQ_V1, "/content/add", body)
    pid = (data or {}).get("id")
    print(f"   [OK] Da dang: https://www.binance.com/square/post/{pid}" if pid
          else "   [OK] Da dang (504 - khong co link tra ve, nhung bai da len).")


# ---------- Main ----------
def resolve_user_id():
    if USER_ID_ENV:
        return USER_ID_ENV
    try:
        cached = USER_ID_FILE.read_text().strip()
        if cached:
            return cached
    except FileNotFoundError:
        pass
    uid = get_user_id(USERNAME)
    USER_ID_FILE.parent.mkdir(parents=True, exist_ok=True)
    USER_ID_FILE.write_text(uid)
    return uid


def main():
    uid = resolve_user_id()
    last = read_state()

    if last is None:
        base = get_new_tweets(uid, None)
        if base:
            write_state(base[-1]["id"])
            print(f"[i] Lan dau chay: dat moc tu tweet moi nhat (id={base[-1]['id']}).")
            print("    Tu gio chi tweet MOI sau thoi diem nay moi duoc dang.")
        else:
            print("[i] Lan dau chay: chua thay tweet nao.")
        return 0

    tweets = get_new_tweets(uid, last)
    if not tweets:
        print("[i] Khong co tweet moi.")
        return 0

    fail_id, fail_n = read_fail()
    for tw in tweets:
        text = strip_trailing_tco(tw.get("text", ""))
        photos = tw.get("_photos", [])
        if photos:
            note = f" (+{len(photos)} anh)"
        elif tw.get("_has_other_media"):
            note = " (co video/gif -> chi dang chu)"
        else:
            note = ""
        print(f"-> Tweet moi{note}: {text[:70]}")

        err = None
        try:
            post_to_square(text, photos)
        except Exception as e:
            print(f"   [!] Loi khi dang: {e}")
            err = e
            if photos and not is_account_error(e):
                print("   -> Thu dang lai chi voi chu...")
                try:
                    post_to_square(text, [])
                    err = None
                except Exception as e2:
                    print(f"   [!] Van loi: {e2}")
                    err = e2

        if err is not None:
            if is_account_error(err):
                print("   -> Loi key/han muc Square: giu tweet lai, lan sau thu tiep.")
                return 1
            if not isinstance(err, SquareError):
                # Loi mang/S3/Square sap tam thoi: khong tinh vao so lan bo qua.
                print("   -> Loi tam thoi: giu lai de lan sau thu lai.")
                return 1
            n = fail_n + 1 if fail_id == tw["id"] else 1
            if n < MAX_FAILS:
                write_fail(tw["id"], n)
                print(f"   -> Loi lan {n}/{MAX_FAILS}: giu lai de lan sau thu lai.")
                return 1
            print(f"   [!] Tweet {tw['id']} loi {n} lan lien tiep -> BO QUA de khong chan cac tweet sau.")
            write_fail(None, 0)
            write_state(tw["id"])
            fail_id, fail_n = None, 0
            continue

        write_fail(None, 0)
        fail_id, fail_n = None, 0
        write_state(tw["id"])
        time.sleep(1)
    return 0


def is_account_error(e):
    return isinstance(e, SquareError) and e.code in SQ_ACCOUNT_ERRORS


if __name__ == "__main__":
    try:
        sys.exit(main() or 0)
    except XApiError as e:
        print(f"[LOI] {e}")
        sys.exit(2)
