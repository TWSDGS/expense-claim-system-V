# 支出報帳系統本次修正重點

## 已實作
1. 付款對象改為互斥三擇一：`employee / advance / vendor`，前端只顯示對應欄位。
2. 稅額與總金額自動計算：
   - `tax_amount = amount_untaxed * 5%` 或 `0`
   - `amount_total = amount_untaxed + tax_amount`
3. 部門固定為 `化安處`：
   - 前端欄位固定顯示
   - Apps Script 寫入時強制覆蓋為 `化安處`
   - 預設 seed 資料原本已是 `化安處`
4. 備註預設值改為：
   - `憑證正本請黏貼於此頁下方；會議請填寫出席人員於用途說明`
5. 新增附件上傳與 PDF 下載：
   - 多張圖片自動排成 A4 2x2 網格頁
   - PDF 附件直接接續在表單後方
6. 新增雲端失敗 fallback：
   - 儲存草稿/送出失敗時改存本機待同步佇列
   - 側欄可手動觸發同步
7. Apps Script schema 增補：
   - `payment_target_type`
   - `attachments_json`

## 這次主要改動檔案
- `expense.py`
- `pdf_gen.py`
- `cache_utils.py`
- `apps_script/Code.gs`

## 注意
- 目前附件實體檔先存在本機 `data/cache/attachments/`。
- `attachments_json` 目前同步的是附件清單 metadata，不是把二進位檔案直接寫進 Google Sheet。
- 若要做到「附件也雲端持久化」，下一步應把附件上傳到 Google Drive，再把 Drive File ID / URL 回寫到 Sheet。
