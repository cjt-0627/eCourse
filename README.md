# eCourse 自架課務整合

本機課務整合網站：手動事件、課程來源選擇、站內提醒與背景同步。兩平台已完成授權帳號的唯讀 route／session 驗收；**公告延遲內文、自然失效後重新登入與長期完整度仍待持續驗證，手機推播尚未完成。**

## 在這台 Mac 啟動

目前已安裝 Python 3.12 虛擬環境、套件與 Playwright Chromium。在 Finder 雙擊 `start.command`，或在專案目錄執行：

```sh
./start.command
```

首次本機啟動會自動建立不可登入的內部 owner，不會在終端要求另一組帳號密碼。網站就緒後會自動在 **Brave** 開啟 **http://localhost:8765**，直接使用校務帳密登入。校務身分驗證成功前不會建立網站 session；密碼只以本機主金鑰加密保存。已初始化時不會覆寫資料或金鑰，並會套用必要 migration。Production 部署仍需管理員在終端明確初始化，避免第一個遠端訪客取得 owner。

`start.command` 使用專案的 `.data` 保存 SQLite，`.secrets` 保存主金鑰與 IPC token。兩個目錄均排除版本控制。關閉網站分頁不停止 worker；終端按 Ctrl+C 會同時停止網站與 worker。電腦關機或休眠時不會執行工作。重複雙擊會重用同一個已啟動的實例，不會再開第二個 worker。

首次同步會完整建立課程與課務快取，因此可能需要數分鐘。後續每小時先檢查兩平台登入狀態；失效時會使用本機加密保存的帳密嘗試重新登入，再以增量模式並行掃描課程，只重查新增、近期未完成或已繳交但尚未出成績的項目。正常網路下以約 10 秒內完成為目標。校方頁面逾時時會保留上次資料並顯示「部分完成」，不會把逾時誤當成沒有課務。

首次同步後到「課程」頁完成來源設定。系統以正式課號合併 eLearn 與 eeclass 的同一門課；有兩個來源時必須選擇其中一個，之後待辦只採用該來源。待辦可篩選「已繳交／未繳交」；已繳交顯示綠色勾勾及平台提供的成績。

先依 [`brave-extension/README.md`](brave-extension/README.md) 载入专用 Brave 扩充功能。在「設定 → 連接」或課務詳情按「使用已登入狀態開啟平台」后，系统会以 15 秒有效且只能使用一次的交接码，把目标平台的登录 Cookie 交给固定 ID 的扩充功能，并在目前 Brave 视窗新增分页。网页 JavaScript 不会收到 Cookie，worker 也不会导出清大 SSO 或其他网域 Cookie。最近一次每小時背景驗證仍有效時会直接開啟；狀態較舊時先重新核對身分。若 session 已失效，系統會先重新登入，必要時要求補填校方驗證碼。

若提示連接埠被占用，請先關閉舊啟動視窗（Ctrl+C）；另一個資料目錄的測試預覽不會被自動取代。Brave 未安裝時會顯示網址供手動開啟。

## 建議驗收順序

1. 登入，進入「待辦」，新增一筆有明確時間的考試、一筆只有日期的事件。
2. 編輯標題並確認時間仍為台灣時間；測試待辦搜尋。校方項目的綠色勾代表「已繳交」，不使用「我已處理／未處理」分類。
3. 在「設定 → 提醒」修改每日／每週摘要時間並保存；「系統狀態」應顯示背景服務正常。
4. 首頁以校務帳密登入；登入後可在「設定 → 連接」更新帳密並重新連接 eLearn 與 eeclass。校務密碼不會回顯。
5. 若校方要求驗證碼，在同頁補填。挑戰只保留約五分鐘；worker 重啟或期限經過後需重試登入。
6. 只有遠端身分核對成功才會顯示已連接。取得課程後，在課程詳情啟用要同步的來源，再按「立即同步」。登入成功與資料完整性會分開記錄。

請分別檢查兩個平台。`partial`／尚未驗證不代表沒有課務；平台頁面結構無法辨識時會明確失敗，保留已有資料。

## 外觀

在「設定 → 外觀」選擇淺色、深色或跟隨系統，切換立即生效；同一頁也能將介面切換為繁體中文或 English。外觀偏好儲存在目前瀏覽器，語言偏好則儲存在帳號中，重新啟動後仍會保留。手機版使用浮動玻璃導覽列。

## 在手機安全開啟

不需要購買網域，也不要將 `8765` 對外做 port forwarding。手機模式使用 Tailscale Serve：eCourse 後端仍只監聽 `127.0.0.1`，Tailscale 在私人 tailnet 內提供 HTTPS，未加入同一個 tailnet 的裝置無法連線。

1. 在 Mac 開啟 Tailscale 並登入。
2. 在 iPhone／Android 安裝 Tailscale，登入同一個 tailnet 並開啟連線。
3. 關閉原本執行中的 `start.command` 視窗，改為雙擊 `mobile.command`。
4. 終端會顯示 `https://...ts.net` 私人網址；在手機瀏覽器開啟並以校務帳密登入。

`mobile.command` 只使用 Tailscale **Serve**，不使用會公開至網際網路的 Funnel。第一次使用時，Tailscale 可能要求在管理頁啟用 tailnet HTTPS；完成後重新執行即可。Mac 必須保持開機、網路連線，且 `mobile.command` 持續執行，手機才能存取及持續背景同步。

## 手動安裝與分開執行

需要 Python 3.12 與 uv：

```sh
uv sync --locked
export PLAYWRIGHT_BROWSERS_PATH="$PWD/.cache/ms-playwright"
uv run playwright install chromium
export ECOURSE_DATA_DIR="$PWD/.data"
export ECOURSE_SECRET_DIR="$PWD/.secrets"
uv run ecourse start --browser brave
```

這台機器若沒有全域 uv，可改用 `.venv/bin/uv`，加上 `--cache-dir .cache/uv`。`start` 會完成首次初始化後啟動網站與 worker，網站就緒才開瀏覽器；使用 `--browser none` 可不開瀏覽器。`dev` 僅啟動兩個獨立子程序；也可在兩個終端以相同環境分開執行：

```sh
.venv/bin/ecourse serve --host 127.0.0.1 --port 8765
.venv/bin/ecourse worker
```

網站認證與校方認證是獨立的。登出網站不會停止 worker；解除平台連接會使舊登入工作失效。

## 管理與驗證指令

```sh
.venv/bin/ecourse doctor
.venv/bin/ecourse self-test
.venv/bin/python -m pytest -q
PLAYWRIGHT_BROWSERS_PATH="$PWD/.cache/ms-playwright" .venv/bin/ecourse doctor --browser
.venv/bin/ecourse migrate
```

管理指令請使用與啟動時相同的 `ECOURSE_DATA_DIR`、`ECOURSE_SECRET_DIR`。未設定時 CLI 預設為 `~/.local/share/ecourse` 與 `~/.local/share/ecourse-secrets`，與 `start.command` 的專案目錄預設不同。

本機模式沒有另一組 eCourse 密碼，也沒有公開註冊或重設密碼 API。既有 P0 `init-vault` 只建立金鑰；完整本機初始化請用 `init`。Production 的管理員 bootstrap 保留終端密碼流程；一旦成功切換為校務帳密登入，就不能重新啟用獨立密碼。

`.env.example` 是設定參考，不會自動載入。資料與金鑰路徑必須為互不包含的絕對路徑。金鑰遺失時不自動建立新金鑰；備份必須保留原主金鑰。不要把 `.data`、`.secrets`、真實帳密、Cookie、私人網頁或備份提交版本控制。

## 尚未完成的範圍

- 兩平台登入後身分 UI、課程目錄、主要內容 route 與程序重啟 Session 恢復已實測；自然失效後重新登入與長期完整度仍需持續驗收。
- eeclass 作業／測驗／公告／教材列表 route 已實測；公告 JavaScript modal 內文與測驗個人結果仍不宣稱完整。
- 圖片驗證碼目前採人工補填，OCR 尚未完成。
- Web Push、PWA 安裝與手機通知、Raspberry Pi 部署、備份還原與金鑰輪替尚未完成。
- 目前以單一擁有者、localhost 開發驗收為範圍，尚未做公開部署驗收。

本機啟動、資料隔離與工作恢復的最新驗收見 `docs/acceptance.md`。平台證據見 `docs/platforms.md`，最早的 P0 基礎驗證紀錄見 `docs/feasibility.md`。完整產品與驗收要求仍以 `ecourse-server-spec.md` 為準。
