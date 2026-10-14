from __future__ import annotations


SUPPORTED_LOCALES = {"zh-Hant", "en"}

# Traditional Chinese is the source language. Keeping the source text as the key
# makes untranslated copy fall back safely instead of exposing an internal key.
EN = {
    '門課程': 'courses',
    '點選課程查看內容與設定來源。拖曳把手可排序，也可聚焦把手後按方向鍵。': 'Open a course for content and source settings. Drag its handle or focus it and use arrow keys to reorder.',
    '排序儲存在此瀏覽器': 'Order saved in this browser',
    '調整排序': 'Reorder',
    '拖曳或按方向鍵調整排序': 'Drag or use arrow keys to reorder',
    '待設定': 'Needs setup',
    '選擇資料來源後，即可查看教材與待辦。': 'Choose a source to view materials and to-dos.',
    '設定課程': 'Set up course',
    '選擇一個來源，或選擇「無」停用這門課的線上資料。': 'Choose a source, or None to disable online data for this course.',
    '登入後首次同步會讀取 eLearn 與 eeclass 的課程清單。': 'The first sync loads your courses from eLearn and eeclass.',

    "主要導覽": "Main navigation", "手機導覽": "Mobile navigation",
    "總覽": "Overview", "待辦": "To-dos", "課程": "Courses", "通知": "Notifications", "設定": "Settings", "登出": "Sign out",
    "連接": "Connections", "提醒": "Reminders", "外觀": "Appearance", "系統狀態": "System status",
    "連接設定": "Connection settings", "提醒設定": "Reminder settings", "外觀設定": "Appearance settings",
    "課程詳情": "Course details", "課務詳情": "Coursework details", "手動事件": "Manual event", "公告": "Announcement", "課務摘要": "Coursework digest",
    "語言": "Language", "繁體中文": "Traditional Chinese", "英文": "English", "介面語言會儲存在你的帳號中。": "The interface language is saved to your account.",
    "顯示模式": "Theme", "淺色": "Light", "深色": "Dark", "跟隨系統": "Use system setting",
    "立即同步": "Sync now", "更新課程": "Refresh courses", "今天": "Today", "未來七天": "Next 7 days", "已逾期": "Overdue", "待確認": "Needs confirmation",
    "最近待辦": "Upcoming", "平台狀態": "Platform status", "最近公告": "Recent announcements", "未來七天沒有待辦。": "No to-dos in the next seven days.",
    "尚未連接平台。": "No platform connected.", "連接設定": "Connection settings", "尚無公告。": "No announcements.",
    "最近同步嘗試：": "Latest sync attempt: ", "最近完整成功：": "Latest complete success: ", "下次排程：": "Next scheduled: ",
    "尚無記錄": "No record", "尚未完整成功": "No complete success yet", "使用已登入狀態開啟": "Open with signed-in session",
    "未指定課程": "No course", "期限未提供": "No due date", "待辦": "To-do", "未命名公告": "Untitled announcement", "已讀": "Read", "未讀": "Unread",
    "搜尋": "Search", "標題或課程": "Title or course", "繳交狀態": "Submission status", "全部": "All", "未繳交": "Not submitted", "已繳交": "Submitted",
    "新增考試／事件": "Add exam/event", "繳交狀態待確認": "Submission status unknown", "未命名項目": "Untitled item", "手動建立的考試／事件": "Manually created exam/event",
    "來源：": "Source: ", "校方狀態：": "School status: ", "成績：": "Grade: ", "編輯": "Edit", "刪除": "Delete", "目前沒有待辦。": "No to-dos yet.",
    "沒有符合目前篩選條件的待辦。": "No to-dos match the current filters.", "新增手動事件": "Add manual event", "編輯手動事件": "Edit manual event",
    "標題": "Title", "日期精度": "Date precision", "日期與時間": "Date and time", "只有日期": "Date only", "尚未確定": "Not decided", "日期時間": "Date and time", "取消": "Cancel", "儲存": "Save",
    "請先選擇課程資料來源": "Choose a course data source", "相同課號已合併成同一門課。每門課請選擇 eLearn、eeclass，或選擇「無」表示不使用線上平台；待辦只會使用你選定的平台資料。": "Courses with the same code are merged. Choose eLearn, eeclass, or None for each course; to-dos use only the selected source.",
    "課號待確認": "Course code unknown", "尚未選擇": "Not selected", "不使用線上平台": "No online platform", "未命名課程": "Untitled course", "學期未提供": "No term",
    "目前使用": "In use", "選擇此來源": "Select this source", "無": "None", "目前不使用線上平台": "Not using an online platform", "這門課不使用平台": "Do not use a platform for this course", "查看課程詳情": "View course details", "進入課程": "Open course", "全部課程": "All courses",
    "只看這門課的待辦": "View this course's to-dos", "課程內容": "Course content", "教材": "Materials", "作業與測驗": "Assignments & quizzes", "成績": "Grades", "公告": "Announcements", "開啟／下載": "Open / download", "課程資料來源": "Course data source",
    "正在取得課程": "Loading courses", "登入後首次同步會讀取 eLearn 與 eeclass 的課程清單。同步完成後，請在這裡選擇同課號課程的資料來源。": "The first sync after sign-in loads course lists from eLearn and eeclass. When it finishes, choose a source for courses with the same code here.", "回總覽查看同步狀態": "Return to overview for sync status",
    "通知": "Notification", "標為已讀": "Mark as read", "沒有通知": "No notifications", "同步問題、期限提醒與摘要會出現在這裡。瀏覽器推播尚未開放，站內通知仍可使用。": "Sync issues, deadline reminders, and digests appear here. Browser push is not available yet; in-app notifications still work.",
    "校務帳密": "School credentials", "帳密加密儲存於本機。更新後會重新連接平台。": "Credentials are encrypted and stored locally. Updating them reconnects the platforms.", "目前帳號：": "Current account: ", "校務帳號": "School account", "校務密碼": "School password", "更新並重新連接": "Update and reconnect",
    "平台連接": "Platform connections", "背景服務運作時，每小時檢查並同步；登入失效會使用本機加密保存的帳密自動重新登入。安装专用 Brave 扩充功能后，「使用已登入狀態開啟」会在目前视窗新增分页。校方要求額外驗證時，仍可能需要補填驗證碼。": "While the background service is running, it checks and syncs hourly. Expired sessions are signed in again with locally encrypted credentials. With the dedicated Brave extension installed, Open with signed-in session opens a new tab in the current window. A verification code may still be required by the school.",
    "登入：": "Sign-in: ", "錯誤代碼：": "Error code: ", "重試": "Retry", "解除": "Disconnect", "校方要求驗證碼時顯示於此；過期後需重試。": "A school verification code will appear here when required; retry after it expires.",
    "摘要與提醒": "Digests and reminders", "每日摘要": "Daily digest", "每日時間": "Daily time", "每週摘要": "Weekly digest", "每週時間": "Weekly time", "安靜開始": "Quiet hours start", "安靜結束": "Quiet hours end", "提前提醒（小時，以逗號分隔）": "Advance reminders (hours, comma-separated)", "儲存設定": "Save settings", "瀏覽器推播尚未開放": "Browser push is not available yet", "目前僅支援站內通知。": "Only in-app notifications are currently supported.",
    "服務狀態": "Service status", "資料庫": "Database", "背景服務": "Background service", "最後回報": "Last heartbeat", "執行中／等待工作": "Active / queued jobs", "尚未回報": "No heartbeat yet", "平台與同步": "Platforms and sync", "工作：": "Job: ", "尚未排程": "Not scheduled", "尚未建立平台連接。": "No platform connection has been created.",
    "正常": "Ready", "尚未就緒": "Not ready", "背景同步正常": "Background sync healthy", "背景同步停止": "Background sync stopped", "狀態未知": "Unknown status", "尚無工作": "No jobs",
    "尚未連接": "Not connected", "正在登入，自動辨識驗證碼中": "Signing in and recognizing the verification code", "自動辨識未完成，請輸入驗證碼": "Automatic recognition was incomplete; enter the verification code", "已登入": "Signed in", "帳密不正確": "Invalid credentials", "平台暫時無法使用": "Platform temporarily unavailable", "登入已過期": "Session expired", "部分完成": "Partially complete",
    "等待中": "Queued", "同步中": "Syncing", "等待驗證": "Waiting for verification", "成功": "Succeeded", "失敗": "Failed", "已取消": "Cancelled",
    "內容": "Content", "未命名": "Untitled", "期限": "Due", "測驗關閉": "Quiz closes", "期限日期": "Due date", "開始": "Starts", "日期": "Date", "發布時間": "Published", "評分狀態": "Grading status", "最後檢查": "Last checked",
    "使用已登入狀態開啟平台": "Open platform with signed-in session", "前往原平台": "Open original platform", "課程來源": "Course sources", "來源": "Source", "使用此來源": "Use this source", "不使用": "Do not use",
    "至": "to", "項課務與事件 · 產生於": "coursework items and events · generated at", "事件": "Event", "這段期間沒有課務項目。": "No coursework items in this period.", "找不到摘要": "Digest not found", "摘要可能已超過保留期限，或不屬於目前的擁有者。": "The digest may have expired or may not belong to the current owner.",
}


def normalize_locale(locale: str | None) -> str:
    return locale if locale in SUPPORTED_LOCALES else "zh-Hant"


def translator(locale: str | None):
    selected = normalize_locale(locale)

    def translate(text: str) -> str:
        return EN.get(text, text) if selected == "en" else text

    return translate
