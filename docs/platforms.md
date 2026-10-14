# 清華 eLearn / eeclass 平台接入紀錄

最後唯讀核實：2026-10-05（Asia/Taipei）。公開資訊與授權帳號驗收分開記錄；不保存帳密、Cookie、token、原始私人 HTML 或未去識別化截圖。

## 已由公開站點確認

| 項目 | eLearn | eeclass |
| --- | --- | --- |
| 官方入口 | `https://elearn.nthu.edu.tw/` | `https://eeclass.nthu.edu.tw/` |
| 公開登入連結 | Moodle `auth/oauth2/login.php?id=5`，連結含當次 `sesskey`，adapter 由首頁點擊，不硬編碼 | `https://eeclass.nthu.edu.tw/service/oauthRedir` |
| OAuth 主機 | `oauth.ccxp.nthu.edu.tw` | `oauth.ccxp.nthu.edu.tw` |
| eeclass OAuth callback | 不適用 | `https://eeclass.nthu.edu.tw/service/oauth/`，client ID `eeclass`，scope `lmsid userid` |
| 校務登入欄位 | 兩平台導向相同校務 OAuth form：帳號 `#id` / `name=id`、密碼 `#password` / `name=password`、驗證碼 `#captcha_code` / `name=captcha`、圖片 `#captcha_image`、隱藏 `captcha_id`、提交按鈕 `name=action value=login` |
| CAPTCHA | 公開 form 使用 `captchaimg.php?id=...`；每次登入由 Playwright 擷取當下元素 bytes | 同左 |
| 平台技術/公開課程 route | Moodle；公開頁可見 `/course/view.php?id=...`、`/mod/assign/`、`/mod/quiz/`、`/mod/forum/`、`/mod/resource/` | 公開課程資訊為 `/course/info/{id}` |

公開證據來源：兩平台首頁、清大 OAuth 登入表單、清大課務組「eLearn／eeclass 選課資料更新時程與登入指引」、eLearn 官方操作說明。OAuth form 的 action 留空，表示提交回當前 authorize URL；adapter 讓瀏覽器原樣提交，不自行重建 OAuth request。

## Adapter 安全邊界

- Playwright `Browser` 由 worker 建立；adapter 每次使用獨立 `BrowserContext`，絕不讀取 Chrome、Safari 或 Codex profile。
- 帳號與密碼只填入當次校務 form，`LoginResult` 不含憑證。待人工處理時只保留 worker 內的 context、圖片 bytes 與五分鐘期限。
- 單次 attempt 最多提交兩次 CAPTCHA。失敗後使用頁面產生的新圖片與新 challenge ID；不重送舊答案。
- redirect 僅允許 `oauth.ccxp.nthu.edu.tw`、`elearn.nthu.edu.tw`、`eeclass.nthu.edu.tw` 的 HTTPS URL。未知 SSO host 回報結構變更。
- 成功必須在受保護頁同時找到登出控制、platform user/profile ID，並在明確標示的帳號欄位精確比對本次登入帳號，才輸出 storage state。若平台不公開帳號欄位，就回 `identity_not_verified`；HTTP 200、Cookie 存在、form 消失或 redirect 完成都不構成成功。
- storage state 是敏感明文，只在 worker 與 vault 邊界傳遞；adapter 不落盤。worker 負責使用既有 vault 加密後保存。

Worker import path：`ecourse.adapters.create_platform_adapters(browser, captcha_solver=None)`。async 方法為 `begin_login(account, password, storage_state=None, connection_generation=0)`、`answer_challenge(attempt_id, challenge_id, answer, request_id=None)`、`refresh_challenge(attempt_id, challenge_id)`、`discard_attempt(attempt_id)`、`verify_session(storage_state)`、`discover_courses(storage_state)`、`fetch_course(storage_state, remote_course_id)`。worker 在連接 generation 改變時必須立即呼叫 `discard_attempt`；它可重複呼叫，會關閉 context 並清除記憶體中的帳密及冪等結果。

eLearn 已實作 Moodle 課程目錄、課程頁的 assignment/quiz/forum/resource link 發現，以及 `fetch_item_detail` 的作業截止、個人繳交狀態、測驗關閉時間與公告首篇純文字。狀態只接受個人 submission status 的明確字樣；單獨看到分數或 grading status 仍為 `unknown`。測驗的 `Closes` 保存為獨立 closes 欄位，不冒充 due deadline。`/mod/forum/view.php` 是討論區容器，不會偽裝成單篇公告；adapter 只跟進該頁實際出現的同源 `discuss.php?d=...`，以及相同 forum ID 的分頁。每課最多展開 8 個 forum、每 forum 5 頁；超限或缺少可辨識 forum root 時保留已讀記錄並回 partial。公告目前保存純文字，不把來源 HTML/script 直接交給 UI。eeclass 作業 personal report 已實測；公告 modal detail 與測驗個人結果仍保守視為未完整驗證。

課程目錄不再把空的 AJAX 容器視為完整空清單。adapter 等待可解析的 `/course/view.php?id=...` 連結，或 Moodle 明確顯示「No courses／You are not enrolled in any courses／沒有課程／無課程／尚未選修任何課程」；其他空白、載入中或逾時狀態回 partial。若仍顯示 next page 或 load-more 控制，也保留本頁課程並回 partial，不宣稱完整。

## 2026-10-05 授權帳號唯讀驗收

本輪以產品已加密保存的兩平台 session 進行，只讀取頁面，不提交作業、測驗或表單。停止網站程序後重新開啟，兩個既有 session 都能直接進入受保護首頁，證明 context／網站重啟後可恢復。為避免主動破壞有效 session，本輪未強制使 Cookie 失效；「失效後自動重新登入」仍只有 lifecycle 自動測試證據，尚待一次自然失效或使用者同意的受控失效驗收。

| 項目 | eLearn | eeclass |
| --- | --- | --- |
| 受保護首頁 | `/my/index.php` | `/dashboard` |
| 登入身分 selector | `[data-region='user-menu']`／`.usermenu`／`#user-menu-toggle`；同頁有 `/login/logout.php` | `#page-sysbar .navbar-right .dropdown-toggle` 顯示目前使用者；行動版選單為 `#userMenu` |
| 課程目錄 | `/my/index.php` 課程概觀，課程連結 `/course/view.php?id={id}` | dashboard「我的課程」，實際連結 `/course/{id}`，不是 `/course/home/{id}` |
| 作業 | `/mod/assign/view.php?id={id}` | 列表 `/course/homeworkList/{courseId}`，詳情 `/course/homework/{id}`，個人結果 `/homework/report/{reportId}` |
| 測驗 | `/mod/quiz/view.php?id={id}` | 列表 `/course/examList/{courseId}` |
| 公告 | Moodle forum container／discussion | 列表 `/course/bulletin/{courseId}`；項目由 JavaScript modal 載入，尚未證實穩定 detail route |
| 教材 | `/mod/resource/`、`/mod/page/`、`/mod/url/` | 列表 `/course/material/{courseId}`，實際文件 `/media/doc/{id}` |
| 個人狀態 | fixture 證實「目前尚無任何作業繳交」與「已繳交，等待評分中」；測驗頁證實 completed | 作業詳情本身不含個人狀態；report 頁證實「此作業已繳交」「尚未批改」「分數 -」 |

最新 `partial_fetch`（job 排定於 2026-10-05 15:38 Asia/Taipei）的實際分類：

- eLearn coverage 為 `course_catalog`；5 個已選來源中 3 個完整、2 個 detail pass 部分完成，共出現 8 次 `item_detail_timeout`。本輪沒有 `elearn_forum_container_limit` 或 `elearn_forum_page_limit`，不能歸因於論壇上限；也沒有新的 forum selector warning。
- eeclass coverage 為 `course_catalog`、`course_codes`；5 個已選來源中 4 個為 `course_refresh_timeout`，另 1 個先讀到 15 筆作業連結，但因錯用 `/course/home/{id}` 及未辨識 `#fs-content` 回 `eeclass_course_root_unrecognized`，後續 14 個 detail 逾時。這是 route／root selector 不完整與 worker 時限的組合，不是課程目錄失敗。
- 實測後 adapter 已改用 `/course/{id}`、`#fs-content`／`#page-main`，並從作業、測驗、公告、教材四個列表 route 擷取；公告 modal detail 仍保守標示為未完整驗證。
- 修正後的獨立 live probe（不寫資料庫）結果：eLearn session connected、課程目錄 11 門且 complete，抽查課程 71 筆（52 教材、19 公告）、四類 coverage complete、無 warning；eeclass session connected、課程目錄 9 門且 complete，抽查課程 31 筆（9 作業、17 公告、5 教材）、四類 coverage complete、無 warning。測驗列表本課為空，代表 route 成功但不是非空測驗樣本。

去識別化 fixtures 位於 `tests/fixtures/platforms/`。擷取器只在瀏覽器記憶體中 clone 必要 DOM，移除 email、姓名、學號、長 token、query string、內容與真實 ID 後才落盤；`manifest.json` 記錄檔案清單，原始私人 HTML 不落盤。

## 尚待真實帳號唯讀驗證

以下不可由公開頁面證實，因此 adapter 不會假裝完整成功：

- eLearn `/my/courses.php` 的實際空清單標記與課程分頁／load-more 極端情況。
- eeclass 公告 JavaScript modal 的穩定 item ID、內文請求與分頁；測驗詳情及已評分作業的個人狀態頁。
- 身分頁能否顯示校務帳號。現版只接受 `data-account`／`data-username` 或「帳號／Username」標籤旁的精確值；若實測無法穩定取得，回 `identity_not_verified`，不得保存新 session。恢復既有 session 時回傳 platform user/profile ID，worker 必須和連接時保存的 remote ID 比對，否則視為 `identity_mismatch`。
- 帳密錯誤、CAPTCHA 錯誤、鎖定與 2FA 的實際提示文字及分類；未知安全頁回 `parser_changed`，不猜測成功。
- OCR 樣本數為 0，預設 `ManualCaptchaSolver`。尚未達規格的 100 張獨立測試集與 95% 正確率，不宣稱自動辨識。

真實測試時應只用使用者透過產品表單提供的憑證，以自然登入節奏執行；不保存密碼、答案、原始私人 HTML、trace、HAR 或截圖。匿名 fixture 應先移除姓名、學號、Cookie、課程私密內容與完整 session URL。
