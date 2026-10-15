# Q006 — fixture nghiệm thu M0

`table_raw.json` giữ **469 ô dữ liệu** của sheet `cpsaat07` (466 ô có nội dung
và 3 ô chuỗi rỗng được lưu tường minh ở A3/A7/A64), vùng
`A1:I68`, trong workbook public `p4_t04.xlsx`. `source_hash` là SHA-256 của chính
file XLSX, không phải hash JSON. Không cần mở XLSX để chạy đa số contract tests;
test đối chiếu nguồn tự skip nếu không có bộ dữ liệu public tại đường dẫn repo.

`table_parsed.json` giữ cùng dữ liệu và tọa độ, chỉ **hai ô E25/E26 được gán
semantic paths thủ công** để chốt cách trao đổi với team table. Những ô khác vẫn
được giữ, paths chưa biết là list rỗng. Đây không phải parser thật, không chứng
minh parser đã hiểu toàn bộ bảng. `parsed_integrated=false`.

`plan_raw.json` dùng sheet/cell; `plan_parsed.json` dùng full semantic paths.
Cả hai chọn cùng E25/E26. `query_context_*.json` chứa toàn bộ text bảng, hash,
question và AnswerSpec, không chứa nhãn từ `expected.json`.

`expected.json` chỉ dùng trong kiểm tra offline: evidence, hai giá trị và đáp án
tham chiếu `7668`. Test xác nhận hai selectors cùng chọn đúng ô và kiểm tra phép
trừ của fixture; M0 chưa có executor production hoặc dự đoán LLM.

Fixture raw giữ nhãn gốc từ XML, không mô phỏng mọi Excel display format. Việc
đọc styles, tính display values và serialize workbook tổng quát thuộc M1.
