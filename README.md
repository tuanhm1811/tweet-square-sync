# Tweet → Binance Square Sync

Tự động lấy **tweet mới** từ một tài khoản X (Twitter) — kèm ảnh — và **đăng lại lên Binance Square**.
Chạy hoàn toàn miễn phí trên **GitHub Actions** (cron ~15 phút/lần), không cần server riêng.

- Đăng tối đa 4 ảnh/bài (giới hạn của Square).
- Tweet có video/GIF → chỉ đăng phần chữ.
- Nếu upload ảnh lỗi → tự động đăng lại chỉ với chữ.
- Ghi nhớ tweet đã đăng trong `state/last_id.txt` để không đăng trùng.

> **Bảo mật:** Toàn bộ key đọc từ **GitHub Secrets** (mã hóa, không nằm trong code).
> Bạn có thể chia sẻ / fork repo này mà **không lộ key**.

---

## 1. Cần chuẩn bị: 5 secret

| Tên secret | Lấy ở đâu | Mô tả |
|---|---|---|
| `TWITTER_API_KEY` | X Developer Portal | API Key (Consumer Key) |
| `TWITTER_API_SECRET` | X Developer Portal | API Key Secret (Consumer Secret) |
| `TWITTER_ACCESS_TOKEN` | X Developer Portal | Access Token |
| `TWITTER_ACCESS_SECRET` | X Developer Portal | Access Token Secret |
| `BINANCE_SQUARE_OPENAPI_KEY` | Binance Square (creator) | Key gọi OpenAPI của Square |

---

## 2. Lấy 4 key từ X (Twitter)

1. Vào **https://developer.x.com** → đăng nhập bằng tài khoản X.
2. Tạo **Project** và **App** (nếu chưa có).
3. Vào App → tab **Keys and tokens**:
   - Mục **Consumer Keys** → bấm *Regenerate/Reveal* để lấy:
     - **API Key** → dùng cho `TWITTER_API_KEY`
     - **API Key Secret** → dùng cho `TWITTER_API_SECRET`
   - Mục **Authentication Tokens → Access Token and Secret** → bấm *Generate*:
     - **Access Token** → dùng cho `TWITTER_ACCESS_TOKEN`
     - **Access Token Secret** → dùng cho `TWITTER_ACCESS_SECRET`
4. Trong **User authentication settings**, quyền **Read** là đủ (script chỉ đọc tweet).

> ⚠️ **Lưu ý quota:** Script gọi endpoint đọc dòng thời gian người dùng (`/2/users/:id/tweets`).
> Gói **Free** của X API rất hạn chế phần đọc và có thể **không đủ quota** để chạy đều.
> Nếu gặp lỗi `429 (Too Many Requests)` hoặc không đọc được tweet, có thể phải nâng lên gói **Basic** (trả phí).
> Có thể giảm tần suất cron (xem mục 6) để tiết kiệm quota.

---

## 3. Lấy `BINANCE_SQUARE_OPENAPI_KEY`

Key này thuộc chương trình **OpenAPI cho creator của Binance Square** (`binance-skills-hub`) —
không phải API Key giao dịch spot/futures thông thường.

- Nếu bạn **đã có sẵn key** (từ người thiết lập trước, hoặc từ chương trình creator/skill của Binance)
  → dùng lại chính key đó.
- Nếu **chưa có**: key được cấp qua chương trình Binance Square / Creator OpenAPI.
  Liên hệ đầu mối đã cấp key cho bạn, hoặc kênh hỗ trợ creator của Binance để xin cấp.

> Header xác thực script gửi đi là `X-Square-OpenAPI-Key: <key>`.
> Đây chính là giá trị bạn đặt vào secret `BINANCE_SQUARE_OPENAPI_KEY`.

---

## 4. Nạp secret vào GitHub

1. Fork repo này về tài khoản của bạn (hoặc dùng repo của bạn).
2. Mở repo trên GitHub → **Settings** → **Secrets and variables** → **Actions**.
3. Bấm **New repository secret**, thêm lần lượt **5 secret** ở mục 1 (đặt đúng tên, dán giá trị).

> Mỗi secret thêm riêng một lần. Tên phải **khớp chính xác** (viết hoa, có dấu gạch dưới).

---

## 5. Đổi tài khoản X muốn theo dõi

Mở file [`.github/workflows/sync.yml`](.github/workflows/sync.yml), sửa dòng:

```yaml
    env:
      TWITTER_USERNAME: "OnchainDataNerd"   # <-- đổi thành @username của bạn (bỏ dấu @)
```

Commit thay đổi.

---

## 6. Bật và chạy trên GitHub Actions

1. Vào tab **Actions** của repo → nếu thấy thông báo → bấm **I understand my workflows, enable them**.
2. Chọn workflow **“Tweet to Binance Square”** ở cột trái.
3. Bấm **Run workflow** (nút bên phải) để **chạy thử tay** ngay.

**Lần chạy đầu tiên** chỉ *đặt mốc* ở tweet mới nhất và **không đăng gì cả** (để tránh spam lại toàn bộ tweet cũ).
Từ lần sau, chỉ tweet **mới hơn** thời điểm đó mới được đăng.

**Lịch tự động:** cron `5,20,35,50 * * * *` → khoảng **mỗi 15 phút** (giờ UTC, GitHub có thể trễ 10–30 phút lúc cao điểm).
Muốn thưa hơn, sửa dòng `cron` trong `sync.yml`, ví dụ mỗi giờ:

```yaml
    - cron: '0 * * * *'
```

> Workflow cần quyền `contents: write` (đã có sẵn) để tự commit `state/last_id.txt` sau mỗi lần đăng.

---

## 7. (Tùy chọn) Chạy thử trên máy tính

```bash
pip install requests requests-oauthlib

export TWITTER_USERNAME="OnchainDataNerd"
export TWITTER_API_KEY="..."
export TWITTER_API_SECRET="..."
export TWITTER_ACCESS_TOKEN="..."
export TWITTER_ACCESS_SECRET="..."
export BINANCE_SQUARE_OPENAPI_KEY="..."

python tweet_to_square_ci.py
```

> Không commit key vào code. Chỉ đặt qua biến môi trường / GitHub Secrets.

---

## 8. Lỗi thường gặp

| Thông báo | Nguyên nhân / cách xử lý |
|---|---|
| `Thieu bien moi truong/secret: X` | Chưa thêm secret đó, hoặc gõ sai tên. |
| `Khong lay duoc user id (401/403)` | Sai key X, hoặc app chưa cấp quyền Read. |
| Lỗi đọc tweet `429` | Hết quota X API (gói Free) → giảm cron hoặc nâng gói Basic. |
| `code=220009` | Vượt giới hạn 100 bài/ngày của Square. |
| `code=220014` | Vượt giới hạn upload ảnh trong ngày của Square. |
| `code=20013` | Nội dung quá dài / không hợp lệ. |
| Đăng ảnh lỗi | Script tự đăng lại **chỉ với chữ** — không mất bài. |

Muốn xem log: tab **Actions** → chọn lần chạy → mở step **“Dong bo tweet → Binance Square”**.
