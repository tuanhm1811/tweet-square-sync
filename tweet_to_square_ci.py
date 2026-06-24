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

# Binance Square endpoints
SQ_V1 = "https://www.binance.com/bapi/composite/v1/public/pgc/openApi"
SQ_V2 = "https://www.binance.com/bapi/composite/v2/public/pgc/openApi"
POLL_INTERVAL = 3
MAX_POLL = 10

CT_MAP = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
          "gif": "image/gif", "webp": "image/webp"}

# Ma loi hay gap cua Square -> giai thich de hieu
SQ_ERR = {
    "220009": "Da vuot gioi han 100 bai/ngay.",
    "220014": "Da vuot gioi han upload anh trong ngay.",
    "20013":  "Noi dung qua dai/khong hop le.",
}


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


# ---------- Twitter ----------
def get_user_id(u):
    r = requests.get(f"{X_API_BASE}/users/by/username/{u}", auth=oauth, timeout=30)
    if r.status_code != 200:
        sys.exit(f"[LOI] Khong lay duoc user id ({r.status_code}): {r.text[:300]}")
    return r.json()["data"]["id"]


def get_new_tweets(uid, since):
    params = {
        "max_results": MAX_RESULTS,
        "tweet.fields": "created_at,attachments",
        "expansions": "attachments.media_keys",
        "media.fields": "url,type",
    }
    if EXCLUDE:
        params["exclude"] = EXCLUDE
    if since:
        params["since_id"] = since
    r = requests.get(f"{X_API_BASE}/users/{uid}/tweets", params=params, auth=oauth, timeout=30)
    if r.status_code != 200:
        print(f"[!] Loi doc tweet ({r.status_code}): {r.text[:200]}")
        return []
    j = r.json()
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
        out.append(tw)
    return list(reversed(out))  # cu -> moi


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
        raise RuntimeError(f"code={code} msg={j.get('message')} {hint}".strip())
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
def main():
    uid = get_user_id(USERNAME)
    last = read_state()

    if last is None:
        base = get_new_tweets(uid, None)
        if base:
            write_state(base[-1]["id"])
            print(f"[i] Lan dau chay: dat moc tu tweet moi nhat (id={base[-1]['id']}).")
            print("    Tu gio chi tweet MOI sau thoi diem nay moi duoc dang.")
        else:
            print("[i] Lan dau chay: chua thay tweet nao.")
        return

    tweets = get_new_tweets(uid, last)
    if not tweets:
        print("[i] Khong co tweet moi.")
        return

    for tw in tweets:
        text = tw.get("text", "").strip()
        photos = tw.get("_photos", [])
        if photos:
            note = f" (+{len(photos)} anh)"
        elif tw.get("_has_other_media"):
            note = " (co video/gif -> chi dang chu)"
        else:
            note = ""
        print(f"-> Tweet moi{note}: {text[:70]}")

        try:
            post_to_square(text, photos)
        except Exception as e:
            print(f"   [!] Loi khi dang kem anh: {e}")
            if photos:
                print("   -> Thu dang lai chi voi chu...")
                try:
                    post_to_square(text, [])
                except Exception as e2:
                    print(f"   [!] Van loi: {e2} -> giu lai de lan sau thu lai.")
                    break  # khong cap nhat state -> lan sau chay lai tweet nay
            else:
                print("   -> Giu lai de lan sau thu lai.")
                break

        write_state(tw["id"])
        time.sleep(1)


if __name__ == "__main__":
    main()
