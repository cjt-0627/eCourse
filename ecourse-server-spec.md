# eCourse 自架課務整合網站完整開發規格

版本：1.0  
日期：2026-10-04  
文件狀態：新專案開發基線，部分登入與裝置能力待實測  
適用對象：產品負責人、開發者、接手新專案的 AI coding agent

本文件定義一個以 Python 後端整合清華大學 eLearn 與 eeclass 的自架網站。使用者連接校務帳號後，伺服器持續同步課程、作業、測驗及公告，產生每日與每週摘要，並向手機和電腦發送提醒。關閉網站或校方分頁，不得中斷伺服器的登入狀態管理與排程。

文件可獨立複製至新專案作為 `SPEC.md`。它取代舊版桌面應用程式與 iPhone 原生 App 的產品方向；不要求新專案依賴舊專案、瀏覽器擴充套件或 Codex。本文只包含規格，不包含任何真實帳號、密碼、Cookie、推播訂閱或私密金鑰。

## 1 產品決策與假設

### 1.1 使用者已確認的需求

| 項目 | 已確認方向 |
| --- | --- |
| 產品形式 | 手機與電腦皆可使用的網站，可加入手機主畫面 |
| 整合平台 | 清大 eLearn 與 eeclass |
| 資料取得 | Python 後端爬蟲，自行維持校方登入與資料擷取 |
| 帳密保存 | 使用者輸入一次後，由後端保存以支援重新登入；必須加密 |
| 同步 | 固定背景同步，檢查登入是否有效，失效時嘗試恢復 |
| 分頁依賴 | 不要求校方分頁、網站分頁或擴充套件持續開啟 |
| 驗證碼 | 實際登入會遇到圖片驗證碼；希望系統協助辨識與填入 |
| 提醒 | 作業與考試的每日／每週摘要，以及期限提醒 |
| 通知裝置 | 電腦瀏覽器與手機主畫面網頁 App |
| 初期部署 | 開發者筆電，使用 localhost |
| 後續部署 | Raspberry Pi，之後提供可連線的網站 |
| 開發方式 | 另開新專案，先以本規格建立系統 |

### 1.2 本版採用的預設決策

以下是為了讓開發可直接開始而選定的預設，不代表使用者已另外確認每一項。修改時應同步更新驗收案例。

| 項目 | 預設 |
| --- | --- |
| 使用範圍 | 單一擁有者自架，可有多個裝置；不開放公開註冊 |
| 語言與時區 | 繁體中文，Asia/Taipei |
| 課務操作 | 唯讀整合；繳交作業、作答、下載受保護附件由原平台完成 |
| 同步頻率 | 每 60 分鐘，設定範圍 15 至 240 分鐘 |
| 每日摘要 | 每天 08:00，使用者可關閉或改時間 |
| 每週摘要 | 每週日 20:00，整理下週一至週日 |
| 截止提醒 | 有精確時間的項目：截止前 24 小時與 3 小時 |
| 安靜時段 | 每天 23:00 至 07:00，可調整；預設不打破安靜時段 |
| 自動驗證碼 | 預設評估本機辨識；不預設將圖片或帳密傳至外部服務 |
| 摘要方法 | 第一版以結構化資料與規則產生，AI 摘要為後續選配 |
| Raspberry Pi | 建議以 64 位元 Linux 驗證；型號、記憶體與儲存設備待確認 |

若之後要開放多人使用，需新增註冊、隔離、配額、帳號恢復與多租戶測試，不能只把單人版公開註冊打開。

### 1.3 已觀察到的事實與尚未證實的能力

2026-10-04 曾在互動式瀏覽器中，經校務系統帳密及圖片驗證碼成功登入兩平台，並看到個人課程與課務資訊。eeclass 一次驗證碼提交失敗後，換用新的圖片驗證碼成功。

這只能證明當時的互動式登入流程可行，不能證明 Python 已完成登入、登入狀態能跨程序還原、驗證碼辨識可長期自動化，或 Raspberry Pi 已能執行。所有這些能力都要依第 19 節驗證。既有 Codex 瀏覽器登入狀態不屬於新系統的部署資產，不得作為新系統依賴。

## 2 產品目標與範圍

### 2.1 核心使用情境

1. 使用者在 eCourse 連接校務帳號並選擇課程來源，之後每天打開網站即可看到整理好的課務。
2. 使用者關閉 eCourse 與校方分頁，伺服器仍按排程同步。
3. 校方登入失效時，伺服器使用保存的憑證嘗試重新登入，成功後接續工作。
4. 使用者沒有開啟網站時，仍可在已訂閱通知的裝置收到摘要或期限提醒。
5. 遇到無法自動處理的登入挑戰，使用者在 eCourse 補充驗證，完成後恢復同步，不必重新設定所有課程。
6. 筆電版本驗收後，將資料與設定搬至 Raspberry Pi，使用相同的應用程式繼續運作。

### 2.2 第一版必要功能

- eCourse 自身的登入、登出、裝置工作階段管理。
- 校務憑證加密保存、兩平台獨立連線狀態與登入狀態恢復。
- 圖片驗證碼辨識模組、有限重試、人工補填與失敗通知。
- 課程發現、跨平台課程配對、課程來源選擇。
- 作業、平台測驗、公告、基本教材連結的同步及來源追溯。
- 日程總覽、清單、課程詳情、公告閱讀、搜尋與篩選。
- 持久化排程、手動同步、部分失敗處理與快取閱讀。
- 每日／每週摘要、截止與期限異動提醒、站內通知中心。
- PWA 安裝、Web Push 訂閱、多裝置投遞、通知設定。
- localhost 啟動、Raspberry Pi 部署設定、備份、恢復與基本監控。

第一版完整交付必須涵蓋背景同步及推播。可以分階段開發，但不能以只有 Dashboard 的版本宣稱整體需求完成。

### 2.3 後續功能

- 公告的 AI 摘要與考試候選事件擷取。
- 行事曆匯出、外部行事曆整合、額外通知管道。
- 多使用者服務、成績彙整、進階教材閱讀與檔案管理。
- 更進階的課程合併建議與個人讀書計畫。

### 2.4 不在本版範圍

- 原生 iOS／Android App、桌面封裝、瀏覽器擴充套件。
- 自動作答、交作業、上傳、發文或修改校方資料。
- 把使用者的瀏覽器當成唯一爬蟲執行環境。
- 永不失效的登入、任何條件下都可辨識的驗證碼，或保證立即送達的通知。
- 在後端關機、休眠或斷網時仍執行新的資料同步。
- 將聊天中的帳密寫入程式、文件、測試、映像檔或版本控制。

## 3 使用流程

### 3.1 首次啟動與系統登入

1. 管理者在本機終端執行初始化指令，建立 eCourse 擁有者帳號與獨立密碼。
2. 開啟 localhost 網站，使用 eCourse 帳號登入。
3. 首次畫面說明資料保存在目前主機，主機需持續運作才能同步。
4. 顯示「連接校務帳號」，使用者在 eCourse 的連接表單輸入校務帳密。
5. 帳密送至自己的 Python 後端，加密保存；後端向清大 SSO 登入。
6. 各平台驗證遠端帳號身分後，顯示連接結果。另一平台失敗不阻塞已成功平台。
7. 抓取課程目錄，使用者選擇每門課使用的平台，執行第一次內容同步。
8. 顯示每日／每週摘要與提醒設定；使用者主動點擊後才請求裝置通知權限。

系統登入與校務登入是兩套生命週期。eCourse 登出不登出校方、不停止後端同步；「解除平台連接」才停止該平台同步並刪除其登入狀態。

### 3.2 一般使用

開啟網站時先讀資料庫，立即顯示最後成功同步的資料，不等待爬蟲。頁面可查閱平台狀態、同步時間、錯誤及下一次排程。按「立即同步」建立背景工作，同一平台正在同步時回傳現有工作，不重複啟動。

點選作業、測驗或公告可查看已同步內容及來源；「使用已登入狀態開啟平台」只接受對應平台的原始 HTTPS 連結。安装固定 ID 的本机 Brave 扩充功能后，worker 可透过 15 秒有效、单次使用且限定扩充功能来源的一次性交接，导出目标平台网域的最小 Cookie 集合，并在目前 Brave 视窗新增分页。不得把 Cookie 传给网页 JavaScript、导出清大 SSO／其他网域 Cookie，或让交接码重复使用。未安装扩充功能时必须明确提示，不得静默降级为不受控的 Cookie 暴露。

### 3.3 登入失效與補驗證

1. 後端的受保護頁面檢查發現登入失效。
2. 將平台標示為「重新登入中」，暫停需要認證的擷取。
3. 從憑證庫取得帳密，開始新的校務 SSO 流程。
4. 若圖片驗證碼可辨識，在重試上限內提交；若需要人工輸入，建立短期挑戰。
5. 站內通知和已啟用的推播提醒使用者開啟「連接設定」。
6. 使用者輸入驗證碼或一次性驗證碼，後端在原本登入交易中提交。
7. 驗證遠端身分成功後保存更新的登入狀態，接續同步並清除故障提醒。

人工補驗證是恢復途徑；如果它頻繁發生，代表自動登入目標尚未達成，不可改稱完全自動化。

### 3.4 更換密碼或帳號

- 更換校務密碼：在設定重新輸入後進行驗證；失敗時保留明確的待修復狀態，不反覆使用舊密碼。
- 更換遠端帳號：建立新的帳號命名空間，取消舊帳號工作及待發通知，不依課名合併舊資料。
- 解除連接：立即遞增連接版本、停止工作、刪除該平台 Session；若憑證仍供另一平台使用，保留憑證並說明。
- 刪除所有校務憑證：停止兩平台連接，刪除加密帳密與登入狀態。歷史課務可另選保留為封存資料或刪除。

## 4 網站畫面與互動

### 4.1 導覽

電腦使用側邊導覽；手機使用適合拇指操作的底部導覽，主要入口為總覽、待辦、課程、通知、設定。視覺以清楚的繁體中文、留白、狀態標籤為主，預設跟隨系統明暗模式。

### 4.2 畫面需求

| 畫面 | 必要資訊與操作 |
| --- | --- |
| 登入 | eCourse 帳密、錯誤訊息；不得誤導成校方官方登入 |
| 總覽 | 今日、未來七天、已逾期、待確認，平台狀態、最近公告、同步時間 |
| 待辦 | 作業／測驗／手動考試，日期排序，課程與平台篩選，狀態篩選 |
| 行程視圖 | 待辦頁可切換每日分組；月曆格不是第一版必要條件 |
| 課程 | 使用中、待選擇、不使用；顯示各平台來源與配對設定 |
| 課程詳情 | 作業、測驗、公告、教材連結、課程來源與最後更新 |
| 項目詳情 | 完整標題、來源、期限精度、繳交狀態、檢查時間與原始連結 |
| 公告詳情 | 清理後內文、發布時間、更新時間、原始連結 |
| 通知中心 | 摘要、截止提醒、異動、連線問題，已讀／未讀 |
| 連接設定 | 憑證更新、兩平台登入狀態、驗證碼挑戰、重新連接與解除連接 |
| 通知設定 | 裝置訂閱、測試通知、摘要時間、提前量、安靜時段、隱私模式 |
| 系統狀態 | API 與 worker 狀態、最後同步、最近錯誤、下一次工作、版本 |

### 4.3 必要顯示規則

- 「已登入」與「資料同步成功」分開顯示；登入成功不能等同資料完整。
- 初次沒有資料顯示引導；同步失敗顯示錯誤及快取，不能顯示成沒有課務。
- 狀態未知、部分同步、離線、平台改版都需有具體文字，不只用顏色。
- 日期不完整時顯示「時間未提供」或「期限未提供」，不能補成 23:59。
- 待辦不分「我已處理／未處理」；勾選圖示是唯讀的校方繳交狀態，已繳交使用綠色，並在平台可提供時顯示成績。
- 鍵盤可操作；表單有 label、錯誤說明；360px 手機寬度不需整頁水平捲動。
- 來源頁 HTML 必須消毒；第一版不直接嵌入來源腳本、iframe 或自動載入私人附件。

## 5 系統架構

### 5.1 執行結構

```text
手機 PWA / 電腦瀏覽器
        │ localhost HTTP 或正式 HTTPS
        ▼
反向代理（正式部署） → Python Web API 與頁面
                            │
                            ▼
                     SQLite 資料庫
                 課務 / 工作 / 通知 / 加密憑證
                            ▲
                            │
                    Python 背景 worker
                ┌───────────┼───────────┐
                ▼           ▼           ▼
           登入管理器    同步與解析器    通知派送器
                │           │           │
                └─────┬─────┘           ▼
                      ▼            瀏覽器推播服務
              清大 SSO / 兩平台            │
                                         ▼
                                  裝置 Service Worker
```

網站只展示與設定，不持有校方 Session。背景工作不由網頁計時器、Service Worker 或使用者瀏覽器發動。Web API 與 worker 是不同程序；Web API 重啟不重複排程，worker 停止時頁面仍能讀取快取。

### 5.2 技術基線

以下為初始選型。初始化時驗證可安裝版本並以 lockfile 固定，不在規格宣稱未測試的特定版本組合已相容。

| 層級 | 基線與用途 |
| --- | --- |
| 後端 | Python 3.12，FastAPI、Pydantic、Uvicorn |
| 頁面 | Jinja2、原生 CSS、少量 TypeScript；Vite 僅用於建置靜態資產 |
| PWA | Web App Manifest、Service Worker、Push API、Notifications API |
| 資料 | SQLite、SQLAlchemy 2、Alembic；本機檔案、WAL、短交易 |
| 擷取 | HTTPX、Beautiful Soup；需要 JS／SSO 時使用 Playwright Chromium |
| 憑證 | 成熟加密函式庫提供認證加密；網站密碼使用 Argon2id 雜湊 |
| 排程 | 獨立 Python worker + 資料庫工作表、lease 與原子 claim |
| 推播 | 支援標準 Web Push 與 VAPID 的 Python 函式庫，選型後鎖版 |
| 測試 | pytest、離線 HTTP fixtures、Playwright UI 測試 |
| 部署 | localhost 原生啟動；正式以 Docker Compose 管理 API、worker、反向代理 |

第一版不引入 Redis、Celery、Kubernetes 或大型前端框架作為必需依賴。Node 只需在開發／建置階段存在，正式主機可直接提供建置好的資產。Python 負責核心業務邏輯；舊專案的程式語言比例不是已確認的新專案硬性要求。

### 5.3 邊界與模組

`auth` 管 eCourse 登入；`vault` 管機密加解密；`connections` 管校方連接；`adapters` 管平台差異；`sync` 管同步一致性；`scheduler` 管工作生命週期；`reminders` 管規則；`push` 管裝置投遞；`web` 管 API 與畫面。

解析器不得自己發送通知，API request 不得直接執行完整爬蟲，通知派送器不得重新解析校方頁面。跨模組以明確的資料模型及 service 呼叫連接。

### 5.4 背景程序與登入挑戰的交接

只有 worker 擁有爬蟲 browser context。API 建立登入工作後回傳 attempt ID，worker 執行並把可公開的狀態寫入 DB。挑戰圖片與答案透過受限的本機 Unix domain socket 在 API 與 worker 之間傳遞，worker 持有原始登入交易；正式容器以非公開 runtime volume 共享 socket，權限只允許這兩個服務存取，並驗證服務 token。

API 必須先檢查使用者、attempt 歸屬、challenge ID、連接版本及期限，worker 再檢查一次。答案只進記憶體，不寫入 DB／佇列／檔案。相同 answer request ID 在該挑戰存活期間冪等；已消耗的答案不可重送。worker 不可達時回 503，交易失效回 410。socket 不能變成公開的任意 browser 控制 API。

爬蟲、排程 heartbeat 及通知派送使用相互隔離的非阻塞任務；OCR 等 CPU 工作在受限 executor 執行。等待人工挑戰不佔住整個排程器；同時最多保留一個待補填的 browser context，其他已有效的 HTTP 同步與推播仍可繼續。第一版的本機程序契約以 macOS／Linux 為基線。

## 6 憑證與校方登入

### 6.1 三類認證資料

| 類型 | 保存方式 | 使用者可見內容 |
| --- | --- | --- |
| eCourse 密碼 | 不可逆雜湊 | 是否已設定，不回傳原文 |
| 校務帳密 | 可解密的認證加密，僅 worker 按需解密 | 遮罩帳號、更新時間、是否已保存 |
| 校方 Session | 加密保存由後端自行登入建立的必要狀態 | 有效／需驗證／失敗、最後檢查時間 |

後端需要校務密碼完成登入，因此不能只保存密碼雜湊。加密可降低資料庫單獨外洩的風險，但不能保證已取得主機與金鑰的人無法解密。

加密主金鑰在資料庫外，透過受限權限的 secret file 掛載；資料庫保存 key version、nonce 與密文。不得寫死、提交 Git，或在缺少金鑰時自動產生新金鑰覆蓋舊資料。第一版可用每安裝一把主金鑰，保留輪替版本；輪替需可中斷恢復與驗證，不先刪舊金鑰。

### 6.2 登入狀態機

```text
not_connected → authenticating → connected
                       ├──────→ challenge_required
                       ├──────→ credentials_invalid
                       └──────→ temporarily_unavailable

connected → expired → reauthenticating → connected
                             ├───────→ challenge_required
                             ├───────→ credentials_invalid
                             └───────→ temporarily_unavailable

任何狀態 → disabled（解除連接）
```

`identity_mismatch` 是獨立阻斷錯誤：實際帳號與設定帳號不一致時，不可提交同步結果，清除該次臨時認證並提示重新連接。一般網路錯誤不等同憑證失效；不能每次 timeout 都重新登入。

### 6.3 登入與恢復規則

1. 同步前讀取受保護但低成本的校方頁面，確認登入及可辨識的帳號身分。
2. Cookie 存在、HTTP 200、表單消失或跳轉成功都不足以單獨認定已登入。
3. 優先重用後端保存的 Session，失效後才執行 SSO。
4. 首版由 Playwright 完成已驗證的 SSO 流程；HTTPX 能否承接資料請求由 P0 實测決定，不假設 Cookie 複製必定足夠。
5. 保存必要狀態後，關閉爬蟲 context；下次從加密狀態還原。若必須維持後端瀏覽器程序，也只能由 worker 管理，不能依賴使用者分頁。
6. 校方提供的 OAuth client 與 redirect URI 必須沿用官方入口，不自行假設擁有校方 OAuth client、refresh token 或第三方授權。
7. 新登入成功需原子替換舊狀態；同平台登入鎖避免同步和恢復互相覆蓋。同一校務憑證的兩平台登入先序列化，減少 SSO 互相干擾。
8. 不讀取使用者 Chrome、Safari、Codex 的 Cookie、密碼庫或 profile；新程式自行建立認證。

Playwright 提供登入狀態保存與還原能力，但具體可保存內容與校方驗證機制仍須實測，且狀態檔應視為機密。[Playwright 認證文件](https://playwright.dev/python/docs/auth)

### 6.4 驗證碼與其他挑戰

定義 `CaptchaSolver` 介面：輸入單張驗證碼圖片，輸出 `candidate`、`confidence`、`solver_version`、`elapsed_ms`，或明確的無法辨識原因。預設為本機實作；使用外部辨識服務必須另行設定及確認資料傳輸範圍。

- 圖片與答案必須綁定同一 `login_attempt_id`、`challenge_id`、校方 context 與連接版本。
- 不硬編碼圖片位數、有效時間或答案；由實測 adapter 定義可接受格式。
- 單次登入最多自動提交 2 次驗證碼；錯誤後取得新圖片，不重送舊答案。
- 低信心、格式不合、未知挑戰時轉為人工補填，不以無限制猜測代替辨識。
- 校方回報帳密錯誤、鎖定或 2FA 要求時，立即停止驗證碼重試；不把帳密錯誤當辨識失敗。
- 挑戰預設最長保留 5 分鐘，或校方更短的有效時間；到期重建登入交易。
- 人工答案由同源 POST 送回；不得放入 URL、日誌、分析服務或推播。
- worker 重啟導致 context 遺失時，舊 challenge 作廢，提供重新產生入口。
- OTP、推播核准及其他無法自動完成的挑戰提供人工流程，不保存一次性驗證碼。
- 遇到非預期安全頁面或新的驗證型態，顯示需人工處理，不停用校方或瀏覽器安全檢查。

更新共用校務憑證時，所有引用它的連接同步遞增 generation，取消舊登入交易。新密碼驗證失敗不得使舊工作把連接重新標為正常；UI 清楚提示新憑證待修復。

「在聊天中由助理辨識一次驗證碼」不能算作產品的自動辨識實作。正式系統不得要求 Codex 常駐才能登入。

### 6.5 重試與告警

網路暫時失敗以 1、5、15 分鐘退避重試，同一時段合併重複工作。驗證碼達上限立即進入 `challenge_required`，不得每小時重跑兩次猜碼；只有新答案、使用者重試或明確的新恢復條件才解除。帳密錯誤需使用者更新憑證後重試。

登入故障進入新狀態時建立一次通知；持續同一故障最多每 24 小時提醒一次。成功恢復後清除故障狀態，保留診斷歷史。

## 7 平台擷取與資料解讀

### 7.1 Adapter 契約

兩平台皆實作下列操作，回傳標準資料與 coverage，而不是直接寫入 UI：

```python
class PlatformAdapter:
    async def verify_session(self, session): ...
    async def begin_login(self, credentials): ...
    async def answer_challenge(self, attempt_id, answer): ...
    async def discover_courses(self, session): ...
    async def fetch_course(self, session, remote_course_id): ...
    async def fetch_item_detail(self, session, item_ref): ...
```

`verify_session` 回傳狀態與遠端帳號 ID；登入操作回傳成功、挑戰或結構化錯誤；擷取回傳記錄、來源識別、所涵蓋範圍、警告與是否完整。傳輸與純解析分開，純解析器可用匿名 HTML／JSON fixtures 測試。

### 7.2 平台範圍

| 平台 | 必讀內容 | 注意事項 |
| --- | --- | --- |
| eLearn | 個人課程目錄、課程主題、作業、測驗、公告討論區、教材連結 | 不把首頁近期項目當完整目錄；公告首篇與回覆分開 |
| eeclass | 個人課程目錄、課程作業、個人繳交結果、公告清單及內文、教材連結 | 最近事件只作補充；延遲載入公告需讀實際內文 |

校方登入入口與頁面結構屬外部依賴，URL、selector 與欄位證據要由實際 adapter 驗證。對分頁、延遲載入、空清單與無權限頁面分別處理。

### 7.3 課程配對與來源選擇

- 以學期、正式課號等資訊提出配對建議；只憑名稱相似不得自動合併。
- 邏輯課程 `CourseGroup` 可連到一個或多個 `CourseSource`。
- 正式課號相同的 eLearn、eeclass 來源合併成同一個邏輯課程；沒有明確課號時不得只憑名稱合併。
- 同一邏輯課程一次只選一個主要來源（eLearn 或 eeclass）；選定一邊時另一邊自動停用，所有項目仍保留平台標籤。
- 只有單一來源的新課程可直接使用；發現第二個同課號來源時改為「待選擇」。課程目錄持續掃描，待選課程不深入爬取內容、不產生提醒。
- 已選平台暫時失敗時，不自動改選另一平台。
- 偏好依擁有者、遠端帳號、學期、來源 ID 保存；相同帳號重登不得清空。
- 跨平台同名作業不自動合併。第一版以來源分開顯示，避免誤刪不同項目。

### 7.4 狀態與日期

作業／測驗的遠端完成狀態限定為 `not_submitted`、`draft`、`submitted`、`graded`、`completed`、`unknown`。逾期另行計算，不作為覆蓋完成狀態的值。已評分但沒有提交／完成證據的頁面要由 adapter 明確定義，不只看到分數欄就推斷完成。

必須保存 `remote_status_text`、`status_checked_at` 與可追溯的證據類型。不得根據全班提交人數、作業已打開、下載紀錄或使用者按下外部連結就認定已交。

時間用以下精度表示：`exact`、`date_only`、`unknown`。精確時間存 UTC 與來源時區；只有日期時存 `due_date`，不建立虛構的 `due_at`。校方明示 00:00 就保留 00:00，不改成當日結束。相對日期只有存在可靠參照時間時才轉換，否則保留原文並標示待確認。

考試／測驗可有 `opens_at`、`starts_at`、`closes_at`、`due_at` 與 `duration_minutes`，不可把開放時間當成考試時間。未知日期不參與精確截止排程。

### 7.5 公告中的考試

第一版完整同步公告，支援使用者從公告建立手動考試事件，填入日期、時間、課程及來源連結。手動事件不會因下次同步被覆蓋。

公告自動辨識考試是後續功能；偵測結果先成為 `candidate`，使用者確認後才能納入確定的考試提醒。矛盾、缺少年份或只有「下週考試」時保留不確定性。產品不可宣稱已自動收集教師在所有公告、圖片或附件中提及的考試。

## 8 背景同步與一致性

### 8.1 排程預設

| 工作 | 頻率與行為 |
| --- | --- |
| 課務同步 | 每 60 分鐘，加入 0 至 120 秒抖動，包含登入檢查、目錄檢查與使用中課程 |
| 輕量登入檢查 | 同步前執行；同步暫停但連接啟用時至多每 60 分鐘 |
| 手動同步 | 即時排入佇列；每平台至少間隔 60 秒，與進行中工作合併 |
| 通知規則掃描 | 每分鐘，使用資料庫時鐘欄位與持久化排程 |
| worker heartbeat | 每 30 秒，超過 90 秒未更新顯示背景服務異常 |
| 資料清理與備份 | 每日；詳見第 17 節 |

### 8.2 持久化工作

工作狀態為 `queued`、`running`、`waiting_user`、`succeeded`、`partial`、`failed`、`cancelled`。保存排定時間、開始／結束、attempt、lease owner、lease expiry、錯誤分類、coverage 與連接版本。

worker 以原子交易 claim 到期工作，持有可續期 lease。程序崩潰後 lease 到期可回收；同一平台不允許兩個有效同步工作。worker 重啟、API 多程序或重複請求不能產生多份相同排程。

每個週期排程另外保存 `next_run_at`、`last_evaluated_at` 及最近已處理的區間，不能只靠記憶體 timer 或掃描最近 90 天通知推算。爬蟲排隊時仍要更新 heartbeat 與派送通知；排程啟動延遲的量測不包含已明示的抖動及校方退避時間。

單次請求預設 timeout 30 秒、完整平台工作 10 分鐘、350 次頁面讀取上限。每來源主機同時最多 1 個請求，預設至少間隔 1 秒；遇到 429 遵循 Retry-After，遇到持續錯誤退避。這些是保守初始值，需依資料量與實測調整，不能把達到上限視為完整成功。

### 8.3 提交資料的規則

1. 開始工作時記錄 `owner_id`、遠端帳號、`connection_generation`、課程偏好版本及 parser version。
2. 讀取結果暫存為本次 run 的 staging 記錄，不邊讀邊刪既有快取。
3. 以來源 ID upsert；純格式、抓取時間變更不生成課務變動事件。
4. 完成某個範圍後，短交易提交該範圍並更新 coverage。
5. 一門課失敗保留該課舊資料；另一課成功可提交。UI 顯示各範圍的新鮮度。
6. 提交前再次比對帳號、連接與偏好版本；使用者解除連接或切換來源後，丟棄舊工作結果。
7. 產生確實的資料變動事件與通知候選，透過同一資料庫交易避免漏建。

只有完整掃過的範圍，才可標記舊項目未再出現；連續 2 次完整同步未見才封存。403、登入頁、解析器失效、請求上限或部分清單均不得觸發大量刪除。來源數量異常驟減時標示疑似改版，保留快取並等待檢查。

### 8.4 重新啟動與斷線

- 筆電休眠或斷網後恢復，只補一次到期同步，不能重播所有錯過的每小時工作。
- 同步失敗不清除已驗證快取；標示最近成功時間、失敗原因與下一次嘗試。
- 單一平台故障不阻塞另一平台的排程、摘要或通知。
- 若所有背景服務停止，網站只顯示已知最後狀態，不能宣稱還在同步。

## 9 摘要與提醒規則

### 9.1 時間範圍

所有使用者可見日期及排程依擁有者時區計算，預設 Asia/Taipei；資料庫精確時間存 UTC。區間採左閉右開，避免午夜重複計入。

| 名稱 | 定義 |
| --- | --- |
| 今日 | 當地今天 00:00 至明天 00:00 |
| 明日 | 當地明天 00:00 至後天 00:00 |
| 未來七天 | 從現在至當地七天後同一時間；包含今日剩餘時間 |
| 本週 | 當週星期一 00:00 至下週星期一 00:00 |
| 每週摘要範圍 | 預設星期日寄送下一個星期一至再下一個星期一的日曆週 |

精確期限的已逾期判定為 `now >= due_at` 且沒有已完成證據；日期型期限在該日期的次日 00:00 以後顯示「日期已過，時間未提供」，不宣稱精確逾期時數。

### 9.2 每日摘要

每日預設 08:00 產生，內容包括：今日與明日到期的未完成項目、今日考試、期限已過但仍待處理的項目數，以及自上次成功產生摘要後的新公告與期限異動摘要。同步狀態另列，不能因資料失效就輸出「今天沒有作業」。

推播保持簡短，例如「今天 2 項、明天 1 項待辦，1 則新公告」。完整內容存為站內摘要，點擊後查看。預設沒有新項目且所有來源資料新鮮時不推播，但仍可在通知中心檢視空摘要；使用者可開啟「沒有待辦也通知」。

### 9.3 每週摘要

預設星期日 20:00 產生，下週項目依日期分組，列出課程、名稱、來源、期限與已知完成狀態。未知時間及未確認考試候選獨立列示，不插入確定時間的行程。摘要存範圍起訖與產生時間，之後查看應標示為當時的摘要，不假裝是即時狀態。

### 9.4 個別提醒

| 類型 | 觸發規則 |
| --- | --- |
| 作業／測驗期限 | 精確 deadline 前 24 小時與 3 小時 |
| 日期型期限 | 前一日 18:00 與當日 08:00，明示時間未提供 |
| 已確認考試 | 優先以 starts_at 前 24 小時與 1 小時；只有日期時使用日期型規則 |
| 新項目 | 初次同步只生成總結；後續新增可依設定立即通知，預設納入摘要 |
| 期限變更 | 比較已存在的期限語意值，生成一次變更通知，重算未發提醒 |
| 連接需要處理 | 狀態首次進入需人工處理，或同一故障持續超過 24 小時 |

已完成、已封存、課程停用、個人標記已處理、未選課程與解除連接的項目不發待辦提醒。`unknown` 狀態可提醒，但文案必須是「尚未確認是否完成」，不得寫「你還沒交」。

已過提醒點才首次發現的項目，不重播每個歷史提前量；若尚未到期，最多產生一則「新發現即將到期」提醒。初次同步已逾期的舊作業只進摘要與清單，不群發歷史提醒。

### 9.5 新鮮度與發送前檢查

預設 60 分鐘內成功驗證的相關範圍為 fresh，超過為 stale，超過 24 小時為 severely_stale。判定依實際範圍最後成功時間，不只看平台最後一個成功頁面。

發送前若狀態超過 30 分鐘未確認，優先排入合併的更新工作，最多等 5 分鐘。更新失敗但資料未超過 24 小時，可發出帶「依上次同步資料，狀態待確認」的提醒。超過 24 小時則暫停個別未交提醒，改發一次同步故障通知；摘要保留已知項目並註明資料過期。使用者不應把過期摘要理解為最新校方事實。

最終投遞前再次確認項目仍有效、提醒規則未關閉、期限版本未變、未完成且裝置訂閱啟用。重新確認以資料庫狀態為主，避免每個裝置各爬一次校方頁面。

### 9.6 安靜時段與停機補送

- 安靜時段內仍建立站內通知，推播延至時段結束。
- 若延後會超過截止時間，取消該次截止推播，在下次摘要顯示；設定頁說明此影響。
- 每日／每週摘要在原定時間後 6 小時內可補送一次，標示延遲；超過就跳過，不積壓多日摘要。
- 截止提醒只有在期限前仍有意義才補送；相同項目短時間內多個到期提醒合併。
- 改變時區、提前量或安靜時段後，重算尚未投遞工作，不重播已完成通知。
- 所有時間計算用可注入的 clock 測試；若未來支援其他時區，須涵蓋夏令時間變化。

## 10 Web Push 與 PWA

### 10.1 通知生命週期

1. 使用者按下「啟用這台裝置的通知」。
2. 前端確認安全環境、Service Worker、PushManager 及通知權限能力。
3. 取得通知權限與 push subscription，送到已認證的 API，綁定擁有者及裝置。
4. worker 產生通知事件與每個裝置的 delivery，送往訂閱對應的推播服務。
5. Service Worker 收到 push 後顯示通知；不可把 Web Push 當作隱藏的定時爬蟲。
6. 點擊通知進入 eCourse 對應頁面；網站登入已過期時先登入，再回到允許的內部路徑。

標準 Web Push 可在網站未開啟時接收事件，但到達與顯示仍受裝置、瀏覽器及系統設定影響。這不是「所有瀏覽器完全退出、裝置離線或關機時仍立即收到」的保證。[MDN Push API](https://developer.mozilla.org/en-US/docs/Web/API/Push_API)

### 10.2 裝置要求

| 環境 | 規格 |
| --- | --- |
| 桌面 Chrome／Edge／Firefox | 以實際支援能力檢查與真機推播測試為準 |
| macOS Safari | 以目標作業系統／Safari 組合實測，不能只以另一瀏覽器通過取代 |
| iPhone／iPad | 支援版本的 iOS／iPadOS，加入主畫面並從該 App 啟動後啟用通知 |
| Android | 支援 Web Push 的瀏覽器，依能力提供安裝引導 |
| 不支援或權限被拒絕 | 保留站內通知，不反覆自動請求權限，顯示手動開啟指引 |

iOS／iPadOS 16.4 起支援主畫面網頁 App 的 Web Push，權限請求需由使用者互動觸發。產品仍需在實際目標手機驗證，不只檢查版本數字。[WebKit 官方說明](https://webkit.org/blog/13878/web-push-for-web-apps-on-ios-and-ipados/)

### 10.3 localhost 與手機測試

本機 `http://localhost` 可作為瀏覽器信任的開發環境；手機的 localhost 是手機本身。手機連筆電的 `http://192.168.x.x` 不等同 localhost 安全環境，不能作為正式推播驗收方案。[MDN Secure contexts](https://developer.mozilla.org/en-US/docs/Web/Security/Defenses/Secure_Contexts)

第一階段可完成本機網站、背景同步、站內通知與支援瀏覽器的本機推播測試。手機推播驗收需要手機可達且憑證可信的 HTTPS 位址，可在受控測試網域或正式部署階段完成。採用測試 tunnel 必須連同網站認證與明確主機設定一起配置，不自動公開開發伺服器。

### 10.4 Manifest 與 Service Worker

- Manifest 提供穩定 `id`、名稱、start_url、scope、standalone display、theme color 及必要尺寸圖示。
- Service Worker 處理 push、notificationclick 與版本更新，scope 限於本站。
- 第一版只離線快取公開的應用程式殼層、圖示與靜態資產，不快取帳密、私人 HTML 或 API 回應。
- 校方斷線但 eCourse 可達時可閱讀伺服器快取；手機完全離線時顯示離線頁，不承諾完整私人資料離線閱讀。
- 新版本提示重新載入；不得在使用者填寫驗證碼或設定時無提示刷新。
- 通知連結只接受本站受允許路徑，不使用任意外部 URL 或包含認證 token 的 deep link。

### 10.5 去重與投遞狀態

通知資料分三層：`NotificationEvent` 表示一件提醒，`Notification` 表示站內內容，`PushDelivery` 表示對一個裝置的一次邏輯投遞。

事件唯一鍵範例：

```text
截止：owner + source_item + deadline_revision + rule_id + offset
摘要：owner + digest_type + timezone + period_start + period_end
故障：owner + connection + incident_id + reminder_day
投遞：notification_event + subscription_id
```

DB transaction 同時寫入課務變動與 outbox；worker 以 lease claim delivery。相同事件重跑不得新建另一份站內通知。每裝置以穩定 `tag` 顯示相同事件，降低重送造成的重複通知。

投遞狀態為 `pending`、`sending`、`accepted`、`failed`、`expired`、`cancelled`，另記可選的 `display_reported_at`、`clicked_at`。`accepted` 只表示推播服務接受，不得顯示成使用者已收到或已讀。伺服器在成功傳送後崩潰可能造成重送，系統採至少一次派送與去重，不承諾端到端 exactly once。

通知正文及詳細投遞紀錄到期清理後，仍保留不含正文的去重 ledger，至少直到相關項目封存或事件失效後 90 天；摘要依持久化排程游標防止重算舊區間。不能因清理歷史紀錄而重新發出同一提醒。

### 10.6 訂閱、失敗與隱私

- 一個擁有者可有多個裝置；每個 subscription 只屬於一個 owner。
- 404／410 表示訂閱失效時停用；429 及暫時性 5xx 有界退避，遵循 Retry-After。
- 每 delivery 最多 5 次嘗試；已超過事件有效時間即取消，不在幾天後投遞過期截止提醒。
- 推播 TTL 不超過事件剩餘有效時間，摘要預設最多 6 小時；安靜時段的延遲在入推播服務之前處理。
- 使用者可移除個別裝置、測試通知、暫停全部通知。登出目前裝置預設停用其推播投遞；重新登入可重新綁定。
- VAPID 金鑰需持久保存且與資料庫分開保護；換網域或換金鑰需安排重新訂閱。
- 訂閱 endpoint 與 encryption keys 視為機密，不記入一般日誌。
- 預設鎖定畫面顯示課務數量，使用者可開啟課名與標題；推播永不包含帳密、驗證碼、Cookie、分數或私人公告全文。

## 11 資料模型

### 11.1 共通約束

所有私人記錄含 `owner_id`；外鍵及查詢必須驗證歸屬，即使單人版也不例外。時間使用帶時區的 UTC 值；單純日期使用 date。ID 使用不可猜測的 UUID，遠端 ID 另外保存為字串。migration 必須可重複部署，禁止啟動時直接刪表重建。

### 11.2 主要資料表

| 表 | 核心欄位與用途 |
| --- | --- |
| owners | id、login_name、password_hash、timezone、created_at |
| app_sessions | owner_id、token_hash、device_label、expires_at、last_used_at、revoked_at |
| credential_secrets | owner_id、encrypted_account、encrypted_password、key_version、updated_at；不提供解密讀取 API |
| platform_connections | owner_id、platform、credential_id、remote_account_id、identity_fingerprint、auth_state、generation、last_verified_at、last_success_at、error_code |
| session_states | connection_id、encrypted_state、key_version、state_version、saved_at、observed_expiry；expiry 不是唯一有效性依據 |
| login_attempts | connection_id、generation、state、challenge_id、challenge_type、expires_at、attempt_count；圖片/context 只短期記憶體 |
| course_groups | owner_id、term、display_name、enabled |
| course_sources | connection_id、remote_course_id、course_code、term、title、source_url、last_seen_at |
| course_selections | group_id、source_id、selection_state、preference_version |
| learning_items | source_course_id、remote_item_id、kind、title、body_sanitized、source_url、remote_status、remote_status_text、status_checked_at、due_at、due_date、date_precision、opens_at、starts_at、closes_at、duration_minutes、deadline_revision、content_hash、last_seen_at、archived_at |
| announcements | source_course_id、remote_id、title、body_sanitized、published_at、source_updated_at、source_url、content_hash、last_seen_at |
| course_materials | source_course_id、remote_id、section_title、kind、title、source_url、last_seen_at；只保存可顯示的教材索引 |
| manual_events | owner_id、group_id、kind、title、starts_at／date、date_precision、source_announcement_id、source_url、confirmed_at |
| item_preferences | owner_id、item_id、handled_at、muted_until；不覆寫遠端狀態 |
| announcement_reads | owner_id、announcement_id、read_at、read_content_hash |
| sync_jobs | owner_id、connection_id、generation、preferences_version、state、scheduled_at、lease_owner、lease_until、attempts、started_at、finished_at、error_code |
| sync_scopes | job_id、course_source_id、scope_type、complete、fetched_count、last_success_at、warnings |
| schedule_states | owner_id、schedule_key、next_run_at、last_evaluated_at、last_period_end、configuration_version |
| item_changes | owner_id、item_id、change_type、old_value、new_value、revision、detected_at |
| reminder_rules | owner_id、kind、enabled、offsets、local_time、weekday、quiet_start、quiet_end、privacy_mode |
| digest_snapshots | owner_id、type、period_start、period_end、generated_at、content、freshness_summary |
| notification_events | owner_id、dedupe_key、type、entity_id、entity_revision、scheduled_at、expires_at、state |
| notifications | owner_id、event_id、title、body、internal_path、created_at、read_at |
| push_subscriptions | owner_id、device_id、encrypted_endpoint、endpoint_hash、encrypted_keys、enabled、created_at、last_success_at、invalidated_at |
| push_deliveries | event_id、subscription_id、state、attempts、next_attempt_at、lease_until、accepted_at、last_error_code |
| notification_dedupe | owner_id、dedupe_key、processed_at、retain_until；不含通知正文的長期去重紀錄 |
| worker_heartbeats | worker_id、version、last_seen_at、active_job_count |
| audit_events | owner_id、actor、action、entity_type、entity_id、result、created_at；不含機密內容 |

`learning_items.kind` 為 assignment／quiz；人工建立的 exam 等事件使用 manual_events。通知引擎透過統一的 `ReminderSubject` 投影讀取兩種來源，不強行假設所有事件都有校方 remote ID。

日期型欄位需保存來源時區與原文；starts／opens／closes 若也只有日期，使用相對應 date 及 precision 欄位，不以時間欄位偷放午夜。table 內列出的欄位是最低契約，實作 migration 必須補齊這些精度欄位及共通時間、版本、歸屬欄位。

### 11.3 唯一鍵與索引

- platform_connections：單人版每 owner、platform 最多一個作用中的連接。
- course_sources：connection、remote_course_id 唯一。
- learning_items：course_source、kind、remote_item_id 唯一；若遠端 ID 平台全域唯一仍保留來源關聯。
- announcements：course_source、remote_id 唯一。
- notification_events：owner、dedupe_key 唯一。
- notifications：owner、event 唯一；push_deliveries：event、subscription 唯一。
- push_subscriptions：endpoint_hash 唯一；更新綁定需通過已認證擁有者檢查。
- 索引包含 owner／時間／狀態、job next_run／lease、item deadline、delivery next_attempt。

對沒有穩定遠端 ID 的內容，以去除短期簽署參數後的來源路徑加上穩定結構識別建立 key；禁止只靠標題或全文 hash 當身分。內容 hash 只判斷內容是否改變。

## 12 API 契約

### 12.1 共通規則

API prefix 為 `/api/v1`，與頁面同源。除健康檢查、網站登入及必要的靜態資產外，皆要求 eCourse 認證。cookie 型認證的所有狀態變更要求 CSRF token 及 Origin 驗證，不開放任意 CORS。

列表採 cursor 分頁，預設 50、上限 100；日期使用 ISO 8601，時間帶 offset。所有錯誤具有 `code`、可顯示的 `message`、`retryable`、`request_id`，不回傳 stack trace、Cookie、上游登入 response 或密碼欄位。受保護頁面及 API 回應使用 `Cache-Control: no-store`。

### 12.2 端點清單

| 方法與路徑 | 功能與結果 |
| --- | --- |
| GET /health/live | 程序存活，無版本及內部敏感細節 |
| GET /health/ready | DB 等必要資源就緒；公開結果只有成功／失敗 |
| POST /auth/login | eCourse 登入，建立 HttpOnly cookie；認證失敗 401，限流 429 |
| POST /auth/logout | 撤銷目前工作階段，停用此裝置投遞 |
| GET /auth/me | 擁有者公開設定與 CSRF 資訊 |
| GET /auth/sessions | 列出裝置工作階段，資訊遮罩 |
| DELETE /auth/sessions/{id} | 撤銷指定工作階段 |
| GET /dashboard | 統計、近期項目、各平台狀態及範圍新鮮度 |
| GET /connections | 兩平台狀態，沒有可解密的秘密 |
| POST /credentials | 建立或更新校務帳密，回傳 credential_id；絕不回顯密碼 |
| POST /connections/{platform}/connect | 以 credential_id 啟動登入，202 + attempt_id |
| POST /connections/{platform}/retry | 有界恢復登入，202 或目前 attempt |
| GET /login-attempts/{id} | 查詢進度及挑戰類型，不回傳校方 token |
| GET /login-attempts/{id}/challenge-image | 擁有者可讀的短期圖片，no-store；過期 410 |
| POST /login-attempts/{id}/answer | 提交答案，含 challenge_id，202；過期 410，版本衝突 409 |
| POST /login-attempts/{id}/refresh | 作廢舊挑戰並建立新挑戰，有節流 |
| DELETE /connections/{platform} | 解除連接並取消該來源工作 |
| DELETE /credentials/{id} | 刪除憑證並撤銷所有引用連接，需回傳影響範圍 |
| GET /courses | 群組與來源清單、待選擇狀態 |
| PUT /courses/{id}/selection | 設定來源，含 expected_version；衝突 409 |
| GET /courses/{id} | 課程詳情與同步 coverage |
| GET /courses/{id}/materials | 教材索引與原始連結，不代理任意附件下載 |
| GET /items | type、course、platform、status、from、to、q、cursor 篩選 |
| GET /items/{id} | 作業／測驗詳情及來源證據摘要 |
| PUT /items/{id}/preference | 個人已處理／提醒暫停設定 |
| GET /announcements | 公告清單與已讀狀態 |
| GET /announcements/{id} | 消毒後公告內文 |
| PUT /announcements/{id}/read | 冪等已讀標記 |
| POST /manual-events | 手動新增已確認考試／事件 |
| GET /manual-events | 列出手動事件，支援日期範圍、課程與分頁 |
| GET /manual-events/{id} | 手動事件詳情與來源 |
| PATCH /manual-events/{id} | 更新手動事件及重新排程提醒 |
| DELETE /manual-events/{id} | 刪除手動事件，取消待發提醒 |
| POST /sync | 排入指定平台或全部平台同步，202 + job_ids；重複時回既有工作 |
| GET /sync/jobs/{id} | 進度、coverage、錯誤、完成時間 |
| GET /notifications | 站內通知清單 |
| PUT /notifications/{id}/read | 冪等已讀，不修改校方完成狀態 |
| GET /digests/{id} | 已保存的摘要與產生當時範圍 |
| GET /settings/reminders | 提醒設定 |
| PUT /settings/reminders | 驗證並保存規則，重算未發通知 |
| GET /push/public-key | 已認證後取得 VAPID public key |
| POST /push/subscriptions | 綁定本裝置訂閱，冪等 upsert |
| DELETE /push/subscriptions/{id} | 停止此裝置投遞 |
| POST /push/test | 向目前裝置送一則測試通知，限流 |
| GET /system/status | 已認證的 worker、同步、資料新鮮度與版本資訊 |

原始校方 Session、任意網頁抓取、解密憑證、任意推播 endpoint 測試都不提供通用 API。管理者備份、restore、主金鑰輪替走本機 CLI，避免第一版增加高權限網頁介面。

### 12.3 回應範例

```json
{
  "data": {
    "id": "item_uuid",
    "platform": "elearn",
    "kind": "assignment",
    "title": "範例作業",
    "due_at": "2026-10-10T15:59:00Z",
    "due_date": null,
    "date_precision": "exact",
    "remote_status": "unknown",
    "handled": false,
    "last_successful_sync_at": "2026-10-04T00:00:00Z",
    "freshness": "stale"
  },
  "meta": {"request_id": "request_uuid"}
}
```

以上日期及標題僅示範資料契約，不是任何人的真實課務。狀態碼使用 400 無效輸入、401 未登入、403 權限／CSRF、404 不存在或不屬於使用者、409 版本衝突、410 挑戰失效、422 欄位驗證、429 節流、503 服務暫不可用。

## 13 安全與資料生命週期

### 13.1 網站存取

- localhost 預設只綁定 127.0.0.1，仍需應用程式登入；不因本機模式取消 CSRF 或 Host 驗證。
- 公開部署必須 HTTPS、可信 Host allowlist、正確的 reverse proxy trust 設定。
- Session cookie 使用 HttpOnly、SameSite=Lax，HTTPS 下使用 Secure；local HTTP 例外僅限明確 localhost 模式。
- 網站 Session 預設閒置 7 天、絕對 30 天到期，可由使用者撤銷。與校方 Session 的有效期無關。
- 系統密碼重設由本機 CLI 完成，重設後撤銷所有 app session；第一版沒有公開忘記密碼郵件流程。
- eCourse 登入及 challenge answer 均限流；錯誤訊息不洩漏額外帳號資訊。
- eCourse 初始管理者必須在主機上建立，禁止公開的先到先註冊初始化頁。

### 13.2 外部內容與請求

- 校方 HTML 是不可信內容，必須消毒並轉成可控呈現格式，移除 script、事件屬性、危險 URL 與 iframe。
- 設定 CSP，私有內容不傳入前端分析工具；第一版沒有第三方分析 SDK。
- 爬蟲只允許經審查的校方 HTTPS 主機與路徑；初始包括兩平台及實測的清大 SSO 主機。
- 不提供任意 URL 抓取；每次 redirect 都重新驗證主機。未知 SSO 主機停止並提示維護。
- HTTP 爬蟲拒絕指向 loopback、link-local、內網及 metadata endpoint 的目的地，並處理 DNS 解析與重新導向的 SSRF 風險。
- 推播 endpoint 僅允許設定的可信 push provider、HTTPS 及正常連接埠；拒絕私人網路位址、任意 redirect 或帶 userinfo 的 URL。
- 學校頁面文字不得成為系統指令。後續 AI 功能的輸入只作待摘要資料，不可觸發工具操作。

### 13.3 機密使用

- 校務帳密只傳給使用者自己的後端及校方正式登入端點。
- 登入頁、驗證碼與受保護流量預設不開錄影、完整 HAR、trace 或 debug 截圖。
- 機密不寫入 URL、例外字串、request body 日誌、metrics、測試 fixtures 或 CI artifact。
- 本機暫存不可避免時用受限權限、短生命週期目錄；刪除只保證邏輯移除，不宣稱 SSD 物理安全抹除。
- 主金鑰與備份解密材料另行保管；應用程式沒有機密解密檢視端點。

### 13.4 保留與刪除

| 資料 | 預設保留 |
| --- | --- |
| 校務帳密、Session | 直到解除連接或使用者刪除；過期 Session 可立即替換 |
| 驗證碼圖片、答案 | 登入交易短期記憶體，完成／到期即清除 |
| 整理後課務與公告 | 保留至使用者刪除，舊學期可封存 |
| 原始 HTML／JSON | 僅同步期間記憶體，不作長期日誌 |
| 同步工作與細部錯誤 | 30 天 |
| 站內通知、摘要、投遞紀錄 | 90 天 |
| 排程游標與去重 ledger | 依第 10.5 節，保留到不再可能重送的範圍 |
| 已失效推播訂閱秘密 | 停用後刪除，保留不含秘密的稽核摘要 |
| 安全與管理稽核 | 90 天 |

刪除帳號資料時須同步取消工作、挑戰與 outbox。備份保留期限內可能仍有舊資料，設定頁及管理文件應說明；資料刪除不能假裝已抹除所有離線備份。

## 14 效能與品質目標

以下是第一版的驗收目標，不是目前已達成的數據。基準資料集為 15 門使用中課程、2,000 個課務項目、2,000 則公告、5 台推播裝置；實際目標 Pi 必須記錄型號、RAM、OS、儲存與瀏覽器版本。

| 指標 | 目標 |
| --- | --- |
| 已有資料的 Dashboard | LAN／localhost 正常環境下 2 秒內可操作 |
| 一般 DB 讀取 API | 基準負載下 p95 小於 500ms，不包含爬蟲 |
| 手動同步回應 | 1 秒內回傳工作 ID 或既有工作 |
| 單平台完整同步 | 正常校方回應及基準課程量下 10 分鐘內完成，超時必須標示部分／失敗 |
| 排程誤差 | worker 正常時在預定時間後 60 秒內開始處理 |
| 瀏覽器資源 | 第一版最多 1 個爬蟲 browser 工作，平台工作序列執行，避免 Pi 記憶體耗盡 |
| 穩定性 | 持續 72 小時運作，無無限重試、工作重疊、通知爆量或未回收 context |
| 資料正確性 | 核心 fixture 的來源 ID、日期精度、完成狀態全部符合預期 |
| 錯誤成功標示 | 0 次把失敗登入、未知完成狀態或部分同步標為完整成功 |

記錄 RSS 峰值、CPU、同步耗時、網路請求量及資料庫成長；資源門檻以實際 Pi 可用記憶體預算制定。達不到時先減少 browser 同時數、改用已驗證 HTTP 路徑、降低同步頻率，不能隱藏資料缺漏。

## 15 可觀測性與錯誤處理

每項背景工作使用 request_id／job_id 關聯結構化日誌，只記錄必要的來源類型、耗時、數量與錯誤碼。帳號、網址 query、公告內文、答案及認證 headers 不進一般日誌。

必要錯誤碼包括 `credentials_invalid`、`session_expired`、`captcha_required`、`captcha_rejected`、`challenge_expired`、`mfa_required`、`identity_mismatch`、`upstream_timeout`、`upstream_rate_limited`、`upstream_unavailable`、`parser_changed`、`partial_fetch`、`stale_generation`、`vault_unavailable`、`push_subscription_expired`。

系統狀態頁顯示 worker heartbeat、兩平台最後成功同步、同步 coverage、最後錯誤、下一次重試、待發通知數與部署版本。網站 API 活著但 worker 停止時，要明確顯示「背景同步停止」。

最低 metrics 為同步成功／部分／失敗數、登入恢復結果、驗證碼辨識與人工介入比例、工作延遲、資料新鮮度、推播 accepted／failed／expired 數、DB 大小與備份最近成功時間。禁止把個人資料作 metrics label。

主機完全離線時本機系統無法主動通知自己的離線狀態。需要停機即時告警時，後續另接外部 uptime monitor，不把同主機排程當成可靠的停機監控。

## 16 部署與設定

### 16.1 開發模式

新專案應提供下列等效命令，名稱為預期介面，尚非現有可執行功能：

```bash
uv sync --locked
uv run ecourse init
uv run ecourse serve --host 127.0.0.1 --port 8765
uv run ecourse worker
```

`init` 建立資料目錄、主金鑰、migration、擁有者及 VAPID keys；密碼透過不回顯的互動提示輸入。校務帳密在網站表單設定，不接受命令列明文旗標。可另提供 `ecourse dev` 同時啟動 API 與 worker，但必須維持單一 worker 的鎖與清理機制。

### 16.2 Raspberry Pi 模式

- 優先測試 64 位元 Debian／Ubuntu 相容環境，依實際設備建立 linux/arm64 容器映像。
- Playwright 官方列有特定 Debian、Ubuntu 的 arm64 支援；這不等同任意 Raspberry Pi OS、32 位元系統或任意瀏覽器套件已驗證可用。[Playwright 系統需求](https://playwright.dev/python/docs/intro)
- API 與 worker 使用相同應用映像、同一主機上的資料卷及分開掛載的秘密檔案。SQLite 不放 NFS／網路共享磁碟。
- 反向代理提供 HTTPS；內部 API 不直接暴露至外網。容器以非 root 運作，設定 restart policy、healthcheck、graceful shutdown 與資源限制。
- Chromium 使用其需要的 sandbox／共享記憶體配置，不把全面關閉 sandbox 當預設解法。
- 建置階段處理前端資產和 Python wheels，不能到 Pi 才假設 x86 套件可以直接執行。
- 啟動前執行一次受控 migration；多個容器不得同時自行 migration。
- 預設不自動開路由器連接埠、不建立公開 tunnel、不購買網域；選定網站公開方式後再配置。

### 16.3 設定契約

| 設定 | 預設或用途 |
| --- | --- |
| ECOURSE_ENV | local／production |
| ECOURSE_BASE_URL | local 為 http://localhost:8765，正式為唯一 HTTPS origin |
| ECOURSE_DATA_DIR | 資料目錄；不以程式工作目錄作隱含依賴 |
| ECOURSE_SECRET_DIR | 主金鑰與 VAPID private key 路徑，權限受限 |
| ECOURSE_ALLOWED_HOSTS | 精確允許的 Host，不用任意萬用字元 |
| ECOURSE_TRUSTED_PROXIES | 只接受實際 reverse proxy 的來源 |
| ECOURSE_LOG_LEVEL | 預設 INFO，仍需全面遮蔽秘密 |
| ECOURSE_BROWSER_EXECUTABLE | 可選，僅在平台驗證過的 browser 路徑使用 |
| ECOURSE_CAPTCHA_PROVIDER | local／manual；local 未通過驗證不得標示已自動化 |
| ECOURSE_PUSH_PROVIDER_HOSTS | 支援之推播服務 allowlist |

摘要、頻率、安靜時段、時區與課程偏好存在 DB，可由網站修改。`.env.example` 只有非秘密範例；密碼、完整訂閱 endpoint 與私密金鑰均不得放入此檔。

## 17 備份與搬遷

### 17.1 備份

每天建立一次一致性備份，保留最近 7 份每日及 4 份每週備份。使用 SQLite backup API 或停止寫入後的受控備份，不能在 WAL 活動時只複製單一 `.sqlite3` 主檔。

備份包含 DB、非秘密設定、schema version、app version、checksum 與備份時間；整份備份加密。主金鑰、備份解密材料及 VAPID private key 另行備份，不將可直接解密的一整組檔案放在公開位置。金鑰只存在同一張 SD 卡不算可恢復備份。

提供 `ecourse backup`、`ecourse restore`、`ecourse doctor` 等等效 CLI。restore 必須檢查完整性、版本與所需金鑰，先進入維護模式，恢復後驗證 DB、解密、登入檢查及排程狀態。首次正式部署前至少完成一次還原演練。

基線目標為每日備份下 RPO 最多 24 小時，備份與金鑰可用時 RTO 2 小時；這是操作目標，需在選定主機實測。只有同主機備份不能涵蓋整台主機故障。

### 17.2 從筆電搬到 Raspberry Pi

1. 在 Pi 上先驗證依賴、ARM64 browser、加密及空資料庫啟動。
2. 停止筆電 worker，維持唯一作用中的同步／推播來源。
3. 建立一致性加密備份，安全移轉 DB 與所需秘密材料。
4. 在 Pi 恢復、migration，校時並設定時區、base URL、Host、HTTPS 與代理。
5. 先用維護模式檢查資料及還原 Session；若校方因環境變化使 Session 失效，透過保存憑證重新登入。
6. 取消過期 lease 與無效 challenge，重算尚未發送的排程，不重播過期通知。
7. 啟動 Pi worker，確認兩平台同步及一次測試推播。
8. 如果 origin 改變，所有裝置重新開啟新網址、登入、安裝 PWA 及訂閱通知；不能假設 localhost 訂閱能搬到新網域。
9. 確認正常後將舊主機維持停用，避免兩台同時通知。

更換主機但保留同一 HTTPS origin 與 VAPID keys 時，可嘗試沿用既有訂閱，仍需真機驗證。校方 Session、browser profile 或瀏覽器二進位不保證跨 OS 可直接搬移。

### 17.3 更新與回滾

發布前建立備份，維護模式中執行 migration，再切換映像並檢查 ready／worker heartbeat。回滾必須考慮 schema 相容性；遇到不可逆 migration，以匹配版本的 DB 備份與映像恢復，不只退回舊程式碼。

## 18 測試策略

### 18.1 測試層次

| 類型 | 內容 |
| --- | --- |
| 純單元測試 | 日期精度、UTC／台北換算、完成狀態、提醒規則、去重鍵、錯誤分類 |
| Adapter fixture | 匿名 HTML／JSON，正常、空清單、分頁、登入頁、403、格式改版與延遲內文 |
| 整合測試 | DB transaction、lease、migration、加密、outbox、部分同步、過期工作取消 |
| UI 測試 | 首次引導、來源選擇、挑戰、通知設定、錯誤與手機版面 |
| 推播模擬 | accepted、404／410、429、5xx、timeout、重送與訂閱撤銷 |
| 真實端到端 | 使用者自己的帳號與裝置，唯讀同步、登入恢復、真正推播 |
| 部署測試 | 筆電與選定 Pi、程序重啟、休眠恢復、備份還原、網域切換 |

CI 不連真實帳號、不保存帳密、不發真實通知。測試資料需要去識別化，不把學號、姓名、完整私有連結或實際 Session 提交版本控制。登入真機測試必須受速率與次數限制，不為了湊樣本反覆快速登入校方。

### 18.2 日期與提醒邊界

必測當日 00:00、23:59、只有日期、沒有日期、跨月跨年、星期日週摘要、完成後取消、草稿仍提醒、unknown 文案、期限延長／提前、日期改未知、撤銷個人已處理、安靜時段跨午夜、修改時區及停機補送。

考試開始時間與作業截止時間各自測試；「開放測驗」不能變成「考試開始」。包含模糊公告「下週考試」的案例必須保持待確認。

### 18.3 故障與競態

- 登入進行中解除連接、換帳號、換密碼，舊工作不能恢復已撤銷的連接。
- 校方回傳登入頁但 HTTP 200，不能視為空課程。
- worker 在寫 staging、提交資料、建立 outbox、傳送 push 前後各階段崩潰。
- 相同工作被重複 claim、lease 過期重領、長工作續期、瀏覽器程序崩潰。
- 已傳送但來不及記錄 accepted 的不確定投遞，確認重送與 tag 行為不被誤報為完全無重複保證。
- 一門課分頁失敗不刪其他頁記錄；其中一平台失效時另一平台照常工作。
- worker 與 API 使用不同版本或 migration 未完成時，ready 不得誤報正常。

### 18.4 安全測試

覆蓋未登入 API、錯誤 owner ID、CSRF、Host／Origin、Cookie flags、Stored XSS、危險來源 URL、SSRF redirect、推播 endpoint 驗證、密碼與 Session 遮蔽、錯誤金鑰、缺少金鑰、加密資料被竄改及解除連接後競態。

需以自動檢查證明 API schema、日誌樣本、錯誤回應、測試 artifacts 和前端 bundle 沒有校務密碼、認證狀態或 private key。這項檢查使用測試秘密標記，不使用真實密碼作比對樣本。

## 19 驗收標準

### 19.1 驗收分級

| 等級 | 可宣稱的成果 | 不可省略 |
| --- | --- | --- |
| A localhost 開發版 | 筆電上的整合網站與獨立背景同步 | 保存狀態、重啟恢復、兩平台資料、站內提醒 |
| B 自動登入驗證版 | 在已測條件下可自動恢復校方登入 | 後端真實登入、驗證碼成功率與人工介入紀錄 |
| C 網站推播版 | 可在指定手機與電腦裝置收到通知 | 可信 HTTPS、裝置訂閱、關閉頁面真機測試 |
| D Raspberry Pi 自架版 | 可由 Pi 持續同步、提醒與恢復 | ARM64 實測、72 小時運作、備份還原與部署文件 |

整體 1.0 需完成 A 至 D。若 B 未達成但 A、C 可用，只能交付明確標示需人工補驗證的測試版，不能將原本的自動登入要求悄悄降級。

### 19.2 功能驗收案例

| ID | 操作與條件 | 通過標準 |
| --- | --- | --- |
| AC01 | 新環境初始化並登入 eCourse | 無真實資料或硬編碼帳密，能建立擁有者並進入引導 |
| AC02 | 連接兩平台 | 各自驗證遠端身分，讀到課程，不把載入完成當登入成功 |
| AC03 | 關閉所有使用者瀏覽器與校方分頁，保留後端 | 到下一個排程仍能同步，資料庫出現有效成功紀錄 |
| AC04 | 停止 API 再啟動，worker 持續運作 | 排程不重複、不依賴頁面，API 可讀既有結果 |
| AC05 | 重啟 worker／主機 | 用已保存狀態恢復；若失效則啟動自動登入，不需重新配對分頁 |
| AC06 | 在自有測試連接使 Session 失效 | 偵測、重登、身分確認、接續同步全程無人工步驟的案例通過 |
| AC07 | 驗證碼辨識失敗 | 不超過 2 次自動提交，顯示挑戰並通知；補填後恢復 |
| AC08 | 帳密錯誤、2FA、鎖定、網路錯誤 | 分別分類，不無限猜碼、不假裝登入成功 |
| AC09 | 抓取兩平台分頁及詳情 | 來源、日期精度、個人提交證據與原平台一致 |
| AC10 | 同步中一課失敗 | 成功範圍更新、失敗範圍留快取、UI 顯示 coverage |
| AC11 | 同步中更換帳號或解除連接 | 舊工作不可寫入，新舊資料不混合，舊提醒停止 |
| AC12 | 選擇單平台／雙平台／不使用 | 只有選定來源進入內容同步與提醒，設定重啟後保留 |
| AC13 | 作業顯示 draft／submitted／unknown | 草稿仍待處理、已交取消提醒、未知不稱未交 |
| AC14 | 今日／每日／每週範圍與日期型期限 | 符合第 9 節，00:00 不被改寫，不捏造未知時間 |
| AC15 | 期限變更及個人已處理 | 舊排程取消、新排程重建，校方狀態不被改寫 |
| AC16 | 重複同步、排程重跑、worker 重啟 | 相同事件只有一筆站內通知與每裝置一筆邏輯 delivery |
| AC17 | 推播服務接受、失敗與訂閱過期 | 狀態正確，accepted 不顯示成已讀，失效訂閱停用 |
| AC18 | 支援的電腦瀏覽器關閉網站分頁 | 收到測試推播並可開啟相應內部頁面 |
| AC19 | iPhone 加入主畫面並允許通知，關閉 PWA | 真機收到推播；拒絕權限仍可閱讀站內通知 |
| AC20 | 未登入者與其他 owner ID 存取私人 API | 不可讀課務、挑戰或秘密，不可操作他人連接 |
| AC21 | 手機視窗、鍵盤及暗色模式 | 主要流程可完成，無遮擋、重要內容不溢出 |
| AC22 | 筆電休眠後恢復 | 補一次同步，依時效合併／跳過過期提醒，無通知洪水 |
| AC23 | 停止 worker 但保留 API | 90 秒後狀態頁顯示背景服務異常 |
| AC24 | 搬遷至 Pi 並變更 origin | 重新登入網站與訂閱通知後可用，筆電 worker 停止 |
| AC25 | Pi 持續運作 72 小時 | 記錄資源與任務結果，無未回收資源或不受控重試 |
| AC26 | 從備份還原到乾淨環境 | 課程設定、資料、必要金鑰可恢復，重新驗證連接及推播 |

### 19.3 自動驗證碼與登入的證據要求

自動辨識須提供與訓練資料分離的匿名測試集與結果；初始評估目標是至少 100 張具代表性且有可靠標籤的圖片，單次辨識正確率達 95%。若樣本不足必須報告實際數量，不能以訓練集正確率代替。

登入恢復另行記錄每平台至少 10 次分散於多日、符合校方速率限制的有效驗證，目標至少 9 次在既定重試額度內自動成功；必須包含 context／worker 重啟與 Session 失效。排除網路中斷的統計要另列，不得隱藏整體成功率。這是早期驗收門檻，不是長期 90% 或 95% 成功的統計保證。

不得為湊齊資料快速反覆觸發登入；可利用經授權的既有匿名樣本、自然登入事件及受控驗證逐步累積。驗證期間記錄失敗原因、是否人工介入、辨識版本與部署環境，不保留帳密。

### 19.4 Definition of Done

- 對應驗收案例與必要測試通過，有可重現的操作紀錄。
- 兩平台資料、登入與通知狀態都能在產品內辨識。
- 沒有秘密進入版本控制、前端、日誌或測試 artifacts。
- 有 lockfile、migration、部署設定、設定範例與操作文件。
- 新環境可依 README 啟動，Pi 可依部署文件安裝，不依賴開發者的瀏覽器。
- 未驗證的裝置或流程列為限制，不用模擬成功替代真機結果。

## 20 開發階段與交付順序

### P0 登入可行性與執行環境

建立最小 Python 原型，分別驗證兩平台 SSO、圖片驗證碼、身分確認、加密 Session 保存、關閉 context 還原、worker 重啟與人工挑戰續接。同時盡早在目標 Pi／ARM64 環境確認 browser 可啟動。

交付 `feasibility.md`，列出實測版本、流程、失敗、驗證碼數據及是否可用 HTTPX。若自動登入或 Pi browser 不可行，先提出具體替代方案與需求差異，再決定後續；不要先花大量時間重畫整個 Dashboard。

### P1 後端骨架與持久化同步

完成資料庫 migration、vault、系統認證、adapter、worker lease、資料 coverage 與來源選擇。先能從 CLI／API 證明關閉分頁仍同步，再加入主要畫面。

交付 localhost 可啟動版本，包含課程、作業、測驗、公告與狀態頁；完成 AC01 至 AC13 的相關案例。

### P2 摘要與提醒引擎

完成日期規則、手動考試、個人已處理、摘要快照、outbox、去重、安靜時段與停機補送。先驗證站內通知，再接外部 push provider，避免用推播到達與否掩蓋排程錯誤。

交付可測試時間的提醒引擎、通知中心與設定頁。

### P3 PWA 與裝置推播

完成 manifest、Service Worker、裝置訂閱、投遞重試、深連結、隱私模式與 HTTPS 測試環境。桌面與手機各留真機驗證紀錄；local-only 環境不足時，明示手機驗收尚待 HTTPS，不宣稱完成。

### P4 Raspberry Pi 正式自架

完成 ARM64 映像、Compose、反向代理、備份還原、從筆電搬遷、72 小時運作及資源測試。停止舊 worker，重新處理 origin 變更造成的裝置訂閱。

### P5 延伸

在穩定版本上加入 AI 公告摘要、考試候選、行事曆匯出或多人方案。每項另立規格與驗收，不隱含擴張第一版的資料傳輸或權限。

本文件不在尚未確認投入人力與 Pi 型號前承諾固定開發週數。每階段以前述交付與驗收條件判定完成。

## 21 新專案結構與接手指引

### 21.1 建議目錄

```text
ecourse-server/
  SPEC.md
  README.md
  pyproject.toml
  uv.lock
  package.json
  package-lock.json
  .env.example
  .gitignore
  alembic.ini
  migrations/
  src/ecourse/
    config.py
    cli.py
    db/
    auth/
    vault/
    connections/
    adapters/
      base.py
      nthu_sso.py
      elearn.py
      eeclass.py
      captcha.py
    parsing/
    sync/
    scheduler/
    reminders/
    push/
    web/
      api/
      templates/
      static/
  frontend/
    styles/
    scripts/
    service-worker.ts
    manifest.webmanifest
  tests/
    unit/
    integration/
    fixtures/
    e2e/
  deploy/
    Dockerfile
    compose.yaml
    proxy/
  docs/
    feasibility.md
    api.md
    deployment.md
    backup-restore.md
    acceptance.md
```

資料庫、secret files、browser 暫存、raw HTML、真實登入截圖與私人備份都放在版本控制外的資料目錄。

### 21.2 舊專案可參考的部分

可評估移植兩平台純解析邏輯、日期處理、公告消毒、課程來源偏好及匿名測試。移植前逐一確認是否與新資料模型、範圍及安全邊界一致。

不沿用「依賴使用者分頁的擷取管道」、擴充套件配對 token、桌面視窗、iOS 原生限制、要求保持分頁開啟的提示、舊版自動提交作業需求，也不搬移舊資料庫中的私密憑證作隱含初始化。

新專案可參考舊程式，但必須有自己的依賴、migration、設定、啟動與測試，不能 import 舊專案絕對路徑。

### 21.3 可直接交給開發者的任務描述

> 依本規格建立新的 eCourse 自架網站。先完成 P0：以 Python 自行登入清大 eLearn 與 eeclass，驗證圖片驗證碼處理、加密 Session 保存、程序重啟恢復與無使用者分頁的背景同步。第一版以單人 localhost 開發，最終部署 Raspberry Pi，網站使用繁體中文並支援 PWA／Web Push。核心為唯讀課務整合、每日／每週摘要及提醒。不得依賴瀏覽器擴充套件、Codex 常駐或把帳密寫死；所有真實憑證透過產品表單或安全初始化流程提供。按照各階段交付，保留未知日期與狀態，驗證失敗要明確回報，不用模擬成功代替真實後端驗收。

## 22 尚待決定與風險處理

| 項目 | 目前處理方式 | 何時必須確認 |
| --- | --- | --- |
| Raspberry Pi 型號、RAM、OS、儲存 | 64 位元 Linux 單 worker 設計；不預設任意硬體皆可 | P0 的 ARM64 smoke test 前 |
| 單人或開放同學使用 | 本規格採單人自架，多裝置 | 若要多人，在公開註冊前另定規格 |
| 網域與外網方式 | localhost 先做；手機正式推播需可信 HTTPS | P3 前 |
| 校方 Session 壽命與跨環境恢復 | 不假定有效天數，保存實測結果 | P0 |
| 自動驗證碼準確度 | 有界自動辨識與人工恢復並行，明示是否達 B 級 | P0 起持續累積，B 級發布前 |
| 2FA 或新驗證流程 | 轉人工挑戰，保留快取，限制重試 | 首次出現時更新 adapter 與測試 |
| 校方頁面改版 | 解析契約、coverage、數量異常偵測，保留舊資料 | 持續維護 |
| 來源平台存取限制 | 遵循站點回應與限流，確認自架整合的適用使用規範 | 正式長期啟用前 |
| AI 摘要服務及資料傳輸 | 第一版不需要外部 AI；日後選定後另確認 | P5 前 |
| 手機與桌面支援矩陣 | 以使用者實際裝置真機驗收 | P3 |
| 備份目的地 | 本地一致性備份先做，異地位置待指定 | 正式部署前 |

以上不確定事項不阻止建立新專案骨架與本機資料模型，但不能跳過登入、Pi 相容性及手機推播的實測關卡。
