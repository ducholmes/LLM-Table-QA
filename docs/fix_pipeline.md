# Kế hoạch sửa pipeline Table QA

## Trạng thái triển khai

Các Phase 0–6 trong tài liệu này đã được triển khai. Regression offline sau
triển khai đạt 106/106 test, bao gồm 20 fixture plan exact-match, raw/parsed
numeric parity, provider retry/rate-limit, submission packaging và các output
shape mới. Hai bản `plan_v1.json` trong source/package giống nhau và toàn bộ
source/tests compile thành công.

Baseline Public data 2 trước thay đổi có 14/30 query thành công; cả 14 answer
đã tạo đều exact-match với `answers.json`. Live regression mới chưa được chạy
vì thao tác đó gửi nội dung workbook tới provider bên ngoài. Khi chạy live,
`answers.json` vẫn chỉ được evaluator đọc sau inference.

## 1. Mục tiêu

Sửa pipeline theo hướng đơn giản nhưng đủ tổng quát để:

- Chạy lại trên private data mà không phụ thuộc query ID, tên workbook hoặc nội dung public data.
- LLM tập trung vào chọn dữ liệu và lập phép tính.
- Tool thực hiện đọc bảng, ép kiểu và tính toán một cách xác định.
- Final answer tuân theo yêu cầu được viết trong query.
- Không loại đáp án đúng chỉ vì regex đoán sai loại output.
- Retry API, repair plan và lỗi output được phân biệt rõ.
- Giữ số operator và schema nhỏ để model dễ sinh plan đúng.

## 2. Cách sử dụng `answers.json`

`data/Public data 2/answers.json` chỉ được dùng để:

- Phân tích các dạng output cần hỗ trợ.
- Tạo regression test và đánh giá offline.
- So sánh accuracy sau mỗi phase.

Không được đưa answer vào prompt inference và không được:

- Hard-code theo query ID.
- Hard-code theo tên file như `p2_t01.xlsx`.
- Hard-code tên category, sheet, năm hoặc con số trong public data.
- Viết rule chỉ khớp nguyên văn một public query.
- Chọn plan hoặc sửa answer bằng cách nhìn ground truth.

Mọi thay đổi phải xuất phát từ contract tổng quát: kiểu runtime, cấu trúc query, kiểu cell và operator.

## 3. Những dạng output thực tế cần hỗ trợ

Đối chiếu 30 query với `answers.json` cho thấy output đều là cấu trúc phẳng:

| Dạng | Số lượng | Ví dụ hình thức |
|---|---:|---|
| Text scalar | 3 | tên group hoặc industry |
| Yes/No | 1 | `Yes` |
| Integer scalar | 8 | `1954`, `4084` |
| Fixed-decimal scalar | 11 | `4.7`, `0.70`, `14.20` |
| Pair/list scalar | 7 | `label, 0.28`, `2020, 9743`, danh sách label |

Trong 7 output dạng list:

- Hai output là cặp number + number.
- Bốn output là string + number.
- Một output là danh sách string có độ dài phụ thuộc dữ liệu.

Kết luận thiết kế:

- Chưa cần record template phức tạp.
- Chưa cần operator `make_record`.
- Chưa cần gọi thêm LLM để format answer.
- Chỉ cần runtime hỗ trợ list các scalar hợp lệ dù khác kiểu, sau đó nối theo thứ tự.
- Chỉ cần metadata rất nhỏ để giữ separator và số chữ số thập phân.

Thiết kế này dựa trên các dạng dữ liệu tổng quát, không dựa vào nội dung cụ thể của public answers.

## 4. Nguyên nhân lỗi hiện tại

### 4.1. `answer_spec` suy đoán quá mức

`parse_answer_spec()` đang dùng regex để gán các kind như `number_pair`, `decimal_pair` và `category`. Điều này gây hiểu sai:

- `group, difference` thành hai number.
- `sheet name, integer` thành hai number.
- Danh sách category biến độ dài thành một category đơn.

Sau execution, `format_answer()` dùng dự đoán này như điều kiện bắt buộc nên có thể loại kết quả đúng bằng `FORMAT_INVALID`.

### 4.2. Runtime cấm mixed scalar list

Kết quả hợp lý như:

```json
["Mexican", 16]
```

bị từ chối vì runtime yêu cầu mọi scalar trong list cùng họ kiểu. Đây là hạn chế không cần thiết đối với final answer tuple.

### 4.3. Numeric string chưa được parse đủ

`as_type: number` chưa xử lý tốt các dạng Excel phổ biến:

```text
$1,882
22,705
18.5%
(1,250)
..
-
N/A
```

### 4.4. Output bị cắt bị báo sai nguyên nhân

Khi Gemini dùng gần hết output budget cho thinking, JSON có thể bị cắt. Pipeline hiện có thể báo `INVALID_PLAN_JSON`, trong khi nguyên nhân gốc là `MAX_TOKENS`.

### 4.5. Format error nằm ngoài plan repair loop

Plan có thể tính đúng nhưng trả list record hoặc shape chưa render được. Lỗi này xảy ra sau execution nên hiện không được planner repair.

## 5. Kiến trúc đích tối giản

```text
Query + Workbook
        |
        v
Table Context
        |
        v
LLM sinh plan JSON
        |
        v
Schema validation
        |
        v
Deterministic tools
        |
        v
Scalar hoặc list[scalar]
        |
        v
Generic formatter
        |
        v
Minimal output validation
        |
        v
submission.csv
```

Phân công trách nhiệm:

- **LLM:** hiểu query, chọn cell/record, lập phép tính, sắp thứ tự output và khai báo precision/separator khi query yêu cầu.
- **Tool:** đọc dữ liệu, ép kiểu, lọc, tính toán, sort, aggregate và lưu evidence.
- **Formatter:** chỉ chuyển scalar/list scalar thành string; không tính toán và không suy đoán semantic.
- **Validator:** chỉ kiểm tra tính hợp lệ kỹ thuật của answer và submission.

## 6. Phase 0 — Tạo baseline trước khi sửa

Trước khi thay đổi code:

1. Lưu error distribution của run hiện tại.
2. Lưu số query thành công, token, số LLM call và thời gian.
3. Dùng `answers.json` tính exact match offline.
4. Chọn các query đại diện cho từng nhóm lỗi làm regression set.

Artifact baseline không được dùng trong inference.

Tiêu chí hoàn thành:

- Có thể so sánh before/after bằng cùng input, config và model.
- Không đánh giá cải tiến chỉ dựa trên số query không crash; phải theo dõi cả exact match offline.

## 7. Phase 1 — Đơn giản hóa output contract

Đây là phase cần làm đầu tiên vì giải quyết trực tiếp các false rejection hiện tại.

### 7.1. Cho phép mixed scalar list

Sửa `RuntimeValue` validation để list sau là hợp lệ:

```json
["full-time employed mothers", 0.28]
```

Mỗi phần tử vẫn phải là một scalar hợp lệ:

- `int`
- finite `Decimal`
- `bool`
- `str`
- `date`
- `null` nếu operator cho phép

Vẫn không cho phép:

- Trộn scalar với record.
- Nested list.
- `NaN` hoặc `Infinity`.
- Object tùy ý trong scalar list.

Không thay `collect` thành một tool format. `collect` chỉ bảo toàn danh sách và thứ tự các giá trị.

### 7.2. Thay `answer_spec` bằng output hint nhỏ

Không dùng các kind suy đoán như `number_pair`, `decimal_pair`, `category` để quyết định pass/fail.

Plan chỉ cần metadata tùy chọn trong `return`:

```json
{
  "return": {
    "ref": "$s5",
    "format": {
      "separator": ", ",
      "decimal_places": 2
    }
  }
}
```

Contract `format` chỉ gồm:

- `separator`: string ngắn, mặc định `, ` cho list.
- `decimal_places`: integer từ 0 đến 10, chỉ áp dụng cho numeric value.

Không cần `mode`, `template`, `fields`, prefix, suffix hay expression trong version đầu.

Thứ tự output chính là thứ tự scalar trong kết quả `collect`. Ví dụ:

```json
["Mexican", 16]
```

được render thành:

```text
Mexican, 16
```

Nếu `decimal_places=2`, chỉ numeric item được format; string không bị thay đổi.

### 7.3. Generic formatter

Quy tắc duy nhất:

- String: trim hai đầu, giữ nguyên nội dung bên trong.
- Integer: decimal digits, không grouping separator.
- Decimal: plain decimal notation; quantize khi plan khai báo `decimal_places`.
- Boolean: `Yes` hoặc `No`.
- Date: ISO date, trừ khi tương lai contract có yêu cầu rõ khác.
- List scalar: render từng item rồi nối bằng `separator`.

Record và list record không phải final answer hợp lệ ở version này. Planner phải `project` field cần thiết và `collect` theo đúng thứ tự.

### 7.4. Minimal validation

Chỉ kiểm tra:

- Answer là string.
- Không rỗng.
- Không có newline.
- Không có `NaN`/`Infinity`.
- Không phải JSON/Markdown explanation do model trả trực tiếp.
- Submission đủ ID, không trùng ID và CSV round-trip được.

Không kiểm tra answer có đúng semantic với query khi inference không có ground truth.

### 7.5. File cần sửa

- `src/table_qa/contracts.py`
- `src/table_qa/context_builder.py`
- `src/table_qa/answer_spec.py`
- `src/table_qa/runner.py`
- `schemas/plan_v1.json`
- `src/table_qa/schemas/plan_v1.json`
- Hai bản `answer_spec_v1.json`, hoặc loại bỏ contract này nếu không còn consumer.
- Tests liên quan đến contracts, formatter, execution và pipeline.

### 7.6. Tiêu chí hoàn thành

- String + number pair chạy được mà không cần record/operator mới.
- Number + number pair vẫn chạy được.
- Variable-length label list chạy được.
- Trailing zero được giữ khi query yêu cầu precision.
- Không còn false failure do `AnswerKind` suy đoán sai.

## 8. Phase 2 — Numeric normalization tổng quát

### 8.1. Một parser dùng chung

Tạo một numeric parser duy nhất và chỉ dùng khi plan yêu cầu `as_type: number`.

Tối thiểu hỗ trợ:

| Input | Runtime value |
|---|---:|
| `1,882` | `1882` |
| `$1,882` | `1882` |
| `(1,250)` | `-1250` |
| `18.5%` | theo percent policy |
| ` 42 ` | `42` |
| `..`, `-`, `N/A`, empty | `MISSING_VALUE` |

Không tự động parse mọi string trong arithmetic. Explicit `as_type: number` giúp phát hiện plan chọn nhầm cột.

### 8.2. Percent policy

Cần có một representation nội bộ thống nhất:

- Text `18.5%` nên thành `Decimal("18.5")` khi bảng thể hiện percentage points.
- Với Excel numeric `0.185` có number format `%`, adapter phải dùng metadata để không scale hai lần.
- Unit metadata phải được giữ qua arithmetic và project.

### 8.3. Locale

Không đoán locale từ một cell riêng lẻ. Mặc định hỗ trợ định dạng số của dataset/config; locale khác chỉ bật qua metadata hoặc config rõ ràng.

### 8.4. Error classification

- Missing marker → `MISSING_VALUE`.
- Chuỗi không phải số → `TYPE_ERROR`.
- Số không finite → `TYPE_ERROR`.
- Unit không tương thích → `UNIT_MISMATCH`.

### 8.5. Tiêu chí hoàn thành

- Currency, grouping separator, percent và accounting negative chạy đúng.
- Raw và parsed mode cho cùng numeric value trên cùng cell.
- Không xuất hiện implicit coercion trong arithmetic.
- Các lỗi q21/q22/q24/q25 hiện tại có thể được giải bằng plan tổng quát.

## 9. Phase 3 — Cập nhật schema và prompt, không thêm operator thừa

### 9.1. Giữ DSL nhỏ

Tiếp tục dùng các operator hiện có:

```text
lookup_cell, select_records, collect, derive,
add, subtract, abs_diff, divide, aggregate,
sort, take, project, compare, filter, argmax, argmin
```

Chưa thêm `make_record`, `format_answer`, `pick_record` hoặc template engine. Chỉ thêm operator khi regression chứng minh operator hiện tại không thể biểu diễn một nhóm bài toán tổng quát.

### 9.2. Schema

- Giữ `additionalProperties: false`.
- Mỗi operator có args schema riêng.
- Thêm `return.format` nhỏ như Phase 1.
- `return.ref` phải trỏ đến step tồn tại.
- Mọi args phải nằm trong `args`.
- Hai bản schema trong repo phải được test là giống nhau.

Nếu provider không hỗ trợ full JSON schema, dùng `json_object` ở API và validate schema cục bộ.

### 9.3. Prompt

Prompt planner cần hướng dẫn ngắn, trực tiếp:

1. Chỉ trả một JSON object, không Markdown.
2. Dùng tool để lấy dữ liệu và tính toán; không đoán literal answer.
3. Dùng `as_type: number` cho numeric string có currency/comma/percent.
4. Với answer nhiều thành phần, `project` các scalar rồi `collect` đúng thứ tự query yêu cầu.
5. Mixed scalar list là hợp lệ; không trả list record làm final answer.
6. Dựa trực tiếp vào query để khai báo `decimal_places` và `separator`.
7. Không thêm unit, `%`, grouping comma hoặc chữ `Answer:` nếu query không yêu cầu.
8. Với danh sách có độ dài theo dữ liệu, không tự giới hạn item count.
9. Raw mode dùng coordinate/range; parsed mode dùng semantic path đầy đủ khi có.
10. Mọi operator argument phải nằm trong `args`.

Chỉ cần các ví dụ cấu trúc tổng quát:

- Scalar lookup/calculation.
- Label + number bằng `collect`.
- Number + number bằng `collect`.
- Variable-length ordered labels.
- Fixed decimal qua `return.format`.
- Numeric string qua `as_type: number`.

Không đưa public answer hoặc tên cụ thể trong public workbook vào example.

### 9.4. Tiêu chí hoàn thành

- Schema ngắn hơn hoặc không phức tạp hơn đáng kể so với hiện tại.
- Model không cần học thêm nhiều operator.
- Tỷ lệ `INVALID_PLAN_SCHEMA` và mixed-list `TYPE_ERROR` giảm.
- Prompt examples đều dùng dữ liệu synthetic.

## 10. Phase 4 — Đưa output formatting vào repair loop

### 10.1. Phân biệt ba loại retry

| Trường hợp | Cơ chế |
|---|---|
| 429, 503, timeout tạm thời | Retry cùng HTTP request |
| JSON/schema/execution/output-shape sai | Planner repair |
| `MAX_TOKENS` | Output retry với budget lớn hơn |

`max_api_attempts` và `max_planner_repair_calls` vẫn là hai giới hạn độc lập.

### 10.2. Output-shape repair

Generic formatter phải chạy trước khi thoát plan loop. Nếu execution trả record/list record hoặc shape không render được:

- Gửi failed plan.
- Gửi error code và step ID.
- Gửi mô tả shape thực tế, không gửi dữ liệu thừa.
- Yêu cầu model sửa bước `project`/`collect` và `return.format`.

Không cần một `render_repair` API riêng trong version đầu. Dùng chung một planner repair call giúp code đơn giản và vẫn chịu giới hạn:

```yaml
max_planner_repair_calls: 1
```

### 10.3. Truncate handling

Thêm `OUTPUT_TRUNCATED` để phân biệt với JSON sai thông thường.

Luồng:

1. Nếu finish reason là `MAX_TOKENS`, retry một lần với output budget lớn hơn.
2. Nếu vẫn bị cắt, trả `OUTPUT_TRUNCATED`.
3. Không dùng plan repair cho một plan chưa được trả đầy đủ.

Benchmark `max_output_tokens` cho Gemini ở khoảng 4,800–6,000 và chọn theo telemetry. Không gắn cứng một giá trị cho mọi provider/model.

### 10.4. Tiêu chí hoàn thành

- Format/output-shape error có tối đa một cơ hội repair như plan error khác.
- Không gọi LLM thêm nếu generic formatter đã thành công.
- MAX_TOKENS có error code và telemetry riêng.
- Tổng logical repair call không vượt config.

## 11. Phase 5 — Provider safety và observability

Giữ và hoàn thiện các hành vi sau:

- Mọi HTTP attempt, retry và repair đều đi qua RPM/TPM limiter.
- 429 theo phút tuân theo `Retry-After` nếu còn deadline.
- Daily quota dừng run với lỗi rõ ràng.
- 503 dùng exponential backoff có jitter.
- 400/422 do request/schema sai không retry cùng request.
- Không tự đổi model nếu fallback chưa được bật trong config.
- Active time không tính quota wait; wall time vẫn ghi đầy đủ.

Mỗi query artifact cần có:

- Provider/model thực tế.
- Call purpose và HTTP attempt.
- Finish reason.
- Token usage, gồm thinking token nếu có.
- Failed plan/error/step ID.
- Typed execution result.
- `return.format` và final answer.
- Active time, quota wait và wall time.

Không log API key, Authorization header hoặc provider body không được sanitize.

## 12. Phase 6 — Test và regression

### 12.1. Unit tests

- Mixed scalar list hợp lệ.
- Scalar + list formatter.
- Decimal quantization và trailing zero.
- Separator được áp dụng đúng.
- Record/list record bị từ chối ở final boundary.
- Minimal final-answer validation.
- Numeric parser cho currency, comma, percent, negative parentheses và missing marker.
- Schema của `return.format`.
- Error classification và retry eligibility.

### 12.2. Synthetic integration tests

Tạo dữ liệu synthetic cho:

- Một scalar string.
- Yes/No.
- Integer và fixed decimal.
- String + number.
- Number + number.
- Variable-length label list.
- Currency arithmetic.
- Percentage-point difference.
- Missing value.
- Raw/parsed parity.
- Plan repair thành công.
- Output-shape repair thành công.
- MAX_TOKENS rồi retry thành công.

Synthetic tests bảo đảm implementation không fit vào public workbook.

### 12.3. Public regression

Sau mỗi phase:

1. Chạy toàn bộ unit/integration test.
2. Chạy lại 30 public queries với cùng model/config khi được phép gửi dữ liệu.
3. Dùng `answers.json` tính exact match offline sau inference.
4. So sánh error distribution, accuracy, token, latency và calls/query với baseline.
5. Điều tra mọi regression trước khi chuyển phase.

Ground truth chỉ xuất hiện trong evaluator, không được đi vào context builder, planner, runner hoặc repair prompt.

## 13. Thứ tự triển khai

Triển khai lần lượt:

1. **Phase 0:** lưu baseline.
2. **Phase 1:** mixed scalar list + generic formatter + minimal validation.
3. **Phase 2:** numeric normalization.
4. **Phase 3:** cập nhật schema và prompt đồng bộ.
5. **Phase 4:** output-shape repair và truncate classification.
6. **Phase 5:** provider safety/telemetry còn thiếu.
7. **Phase 6:** full regression và tinh chỉnh dựa trên error class.

Sau mỗi phase phải chạy test và xem diff/error distribution trước khi làm phase tiếp theo. Không gộp tất cả thay đổi thành một patch lớn.

## 14. Những việc chưa nên làm

Để giữ code đơn giản, chưa triển khai:

- LLM finalizer thứ hai cho mọi query.
- Template renderer tổng quát.
- `make_record` chỉ để ghép label và number.
- Tự động đoán format phức tạp bằng regex đa ngôn ngữ.
- Implicit string-to-number coercion trong arithmetic.
- Dynamic arbitrary Python expression/eval.
- Model fallback tự động không có config.
- Operator mới chưa có failing regression chứng minh nhu cầu.

Chỉ mở rộng các phần này nếu private-like synthetic tests hoặc error artifacts cho thấy thiết kế tối giản không đủ.

## 15. Definition of Done

Pipeline được coi là đạt yêu cầu khi:

- Chạy được với input directory mới, không phụ thuộc sample path.
- Không có code path đặc biệt theo public query ID/file/content.
- Mỗi query có đúng một answer hoặc một error artifact rõ ràng.
- Mixed label/number và variable-length list được xử lý tự nhiên.
- Precision và separator lấy từ plan dựa trên query, không từ ground truth.
- Numeric string phổ biến trong Excel được parse explicit và đúng.
- Raw/parsed mode nhất quán trên fixture tương đương.
- API retry và planner repair hoạt động độc lập, đúng giới hạn.
- Output truncation có root cause riêng.
- `submission.csv` đủ ID, không trùng ID và round-trip thành công.
- Test suite pass.
- Exact match public không giảm; error rate và calls/query được theo dõi.
- Token, latency và lỗi của từng query có thể truy vết từ artifacts.
