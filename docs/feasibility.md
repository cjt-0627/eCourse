# P0 可行性紀錄

測試日期：2026-10-04（Asia/Taipei）

## 本次範圍

P0.1 為執行環境與加密儲存的第一個檢查點，**不是 P0 登入驗收完成**。不使用任何真實校務帳密、Cookie、既有瀏覽器登入狀態或私人課程資料。

| 項目 | 本次證據 |
| --- | --- |
| 主機 | macOS / Darwin，arm64；不是 Raspberry Pi |
| Python | 3.12.8 |
| uv | 0.12.23；lockfile 重建檢查通過 |
| cryptography | 50.0.2 |
| Playwright | 1.63.0 |
| pytest | 9.1.1；20 項通過 |
| 瀏覽器 | Chromium 153.0.8010.12；啟動、關閉 context、加密狀態還原通過 |

## 已驗證

- AES-256-GCM 保存 JSON 狀態，每次加密產生新的 96-bit nonce，保存 envelope 與 key version。
- 平台與帳號命名空間納入 authenticated associated data，跨平台或跨帳號無法解密。
- 測試狀態寫入檔案後，由另一個 Python 程序讀回成功；不是只在同一個物件內 round-trip。
- 篡改密文、換錯金鑰、異常格式、寬鬆金鑰權限、金鑰符號連結均被拒絕。
- 缺少金鑰不自動產生；重複初始化不覆寫；非空資料目錄禁止以新金鑰初始化。
- 寫檔採同目錄臨時檔及 atomic replace；模擬 replace 失敗時保留原資料、清除臨時檔。
- 資料與金鑰預設路徑不依賴工作目錄；設定拒絕相對路徑與巢狀資料／金鑰目錄。
- `doctor --browser` 實際建立 Chromium context、加入虛構 Cookie、加密保存 storage state、關閉 context，再用新 context 還原 Cookie 成功。啟動時明確開啟 Chromium sandbox；過程不瀏覽任何遠端頁面。

本次環境問題：第一次執行時沒有可用 Chromium；安裝下載途中遇到一次 `ECONNRESET`，安裝器重試完成。受限執行沙箱內的瀏覽器探測仍失敗；取得執行權限後，以相同指令在沙箱外測試成功。上述主機限制不計為校方登入失敗，也未藉此關閉 Chromium sandbox。

加密 API 參考 [cryptography AESGCM 官方文件](https://cryptography.io/en/latest/hazmat/primitives/aead/#cryptography.hazmat.primitives.ciphers.aead.AESGCM)。目前是檔案儲存原型；P1 再將 envelope 整合到資料庫與帳號生命週期。尚未實作 key rotation、備份還原流程及正式部署防護。

## 尚未驗證與完成條件

| 能力 | 狀態與下一個證據 |
| --- | --- |
| eLearn SSO | 尚未實作；需 Python 自行登入並驗證遠端帳號 |
| eeclass SSO | 尚未實作；需獨立驗證登入狀態與帳號 |
| HTTPX 可用性 | 未判定；先觀察真實 SSO 流程 |
| 圖片驗證碼 | 人工與 OCR 均未實測；樣本數 0、成功率未知 |
| 校方 Session 還原 | 未驗證；需加密保存真實狀態，重啟後存取受保護頁並確認身分 |
| worker 與人工挑戰 | 未實作；需獨立程序、期限、交易續接與重啟失效處理 |
| 無分頁背景同步 | 未實作；需關閉使用者分頁後持續取得資料的證據 |
| Pi / Linux ARM64 | 未驗證；需指定硬體和作業系統後實測 browser |

下一段先建立安全的本機登入操作路徑，逐平台實測 SSO，保留有限重試與人工驗證，不以虛構 Cookie 測試代替校方登入驗收。
