# LLM Table QA

Hiện đã triển khai **M0 — Contracts**, **M1 — Offline orchestration**,
**M2 — Tool engine**, **M3 — Prompt & validation**, **M4 — Multi-provider integration** và
khung benchmark/evaluation của **M5**, cùng hardening/package của **M6**, theo
[kế hoạch](docs/llm_table_qa_pipeline_plan.md). M1 đọc query và workbook Excel,
dựng full-table context, rồi route qua mock LLM. M2 thực thi plan DSL cục bộ;
M3 thêm prompt có version, kiểm tra/format đáp án, sửa plan tối đa một lần,
ghi CSV khi cả run hợp lệ, và đánh giá nhãn tùy chọn. Chọn OpenRouter/Gemini
bằng YAML config; profile competition khóa đúng OpenRouter/Qwen.

## Chạy với Gemini hoặc OpenRouter lần đầu

Làm theo các bước dưới đây từ thư mục gốc của project. Project tự đọc API key
từ file `.env`; không cần dùng `export` hoặc cài `direnv`. Cần Python 3.11 trở
lên.

### 1. Tạo file `.env`

Chạy hai lệnh sau từ thư mục gốc project để tạo và mở file `.env`. `cp -n` sẽ
giữ nguyên file nếu bạn đã tạo `.env` trước đó.

```bash
cp -n .env.example .env
nano .env
```

Trong file, điền API key cho provider bạn muốn dùng:

- Gemini: thay `replace_with_gemini_key` ở dòng `GEMINI_API_KEY=...`.
- OpenRouter: thay `replace_with_openrouter_key` ở dòng `OPENROUTER_API_KEY=...`.

Lưu trong nano bằng `Ctrl+O`, nhấn `Enter`, rồi thoát bằng `Ctrl+X`. Không gửi
file `.env` cho người khác và không commit lên Git.

### 2. Cài dependency

Tạo môi trường Python và bật nó:

```bash
python3 -m venv .venv
source .venv/bin/activate
```

Cài dependency cho **một** provider bạn định dùng:

Gemini:

```bash
python -m pip install -e '.[gemini]'
```

OpenRouter:

```bash
python -m pip install -e '.[openrouter]'
```

Nếu lệnh tạo môi trường báo thiếu `venv` trên Ubuntu, chạy `sudo apt install
python3-venv`, rồi chạy lại bước này. Khi mở terminal mới, kích hoạt lại môi
trường bằng `source .venv/bin/activate` trước khi chạy project.

### 3. Chạy pipeline

Chạy **một** lệnh tương ứng với provider có key trong `.env`:

```bash
# Gemini
PYTHONPATH=src python -m table_qa.main --config config/benchmark_gemini.yaml

# OpenRouter
PYTHONPATH=src python -m table_qa.main --config config/benchmark_openrouter.yaml
```

Cả hai lệnh đều gọi dịch vụ model để chạy benchmark và có thể phát sinh phí API.
Kết quả cùng log được ghi trong thư mục `runs/`. Nếu báo thiếu credential, hãy
kiểm tra `.env` nằm ở thư mục gốc project và đã điền đúng biến cho provider được
chọn.

## Chạy kiểm tra offline

Cần Python 3.11+, `jsonschema>=4.19,<5` và PyYAML. Mock không cần API key.

```bash
# Cài package và dependency nếu môi trường chưa có.
python3 -m pip install -e .

# Để gọi Gemini hoặc OpenRouter:
python3 -m pip install -e '.[providers]'

# Chạy từ thư mục gốc repo; cũng dùng được khi jsonschema đã có sẵn,
# không cần cài editable package.
PYTHONPATH=src python3 -m unittest discover -s tests -v

# Offline: mock placeholder không dự đoán đáp án thật, nên run này thường kết thúc
# có lỗi FORMAT_INVALID và không tạo submission.csv. Dùng để kiểm tra log/error flow.
PYTHONPATH=src python3 -m table_qa.main --config config/default.yaml

# M5: mock chỉ kiểm tra plumbing, không dùng làm bằng chứng accuracy.
PYTHONPATH=src python3 -m table_qa.benchmark \
  --config config/default.yaml --allow-mock --compare-prompt-b \
  --output-dir /tmp/table-qa-m5-mock

# Sinh hai split synthetic theo workbook; không tune prompt trên holdout.
PYTHONPATH=src python3 -m table_qa.synthetic \
  --output-dir runs/synthetic --dev-tables 4 --holdout-tables 4

# Benchmark E1/E2 và A/B trên dev bằng provider đã cấu hình (có thể phát sinh phí).
PYTHONPATH=src python3 -m table_qa.benchmark \
  --config config/benchmark_openrouter.yaml \
  --queries runs/synthetic/dev/queries.json \
  --data-dir runs/synthetic/dev/xlsx_files \
  --labels runs/synthetic/dev/labels.csv \
  --dataset-name synthetic-dev --compare-prompt-b

# Đánh giá prompt đã chọn một lần trên holdout riêng, không chọn prompt theo kết quả này.
PYTHONPATH=src python3 -m table_qa.benchmark \
  --config config/benchmark_openrouter.yaml \
  --queries runs/synthetic/holdout/queries.json \
  --data-dir runs/synthetic/holdout/xlsx_files \
  --labels runs/synthetic/holdout/labels.csv \
  --dataset-name synthetic-holdout

# Chỉ bật E3/E4 sau khi parser thật tạo đủ JSON có source_hash khớp workbook.
# Thêm: --include-parsed --parsed-dir <parser-output-dir>

# DIRECT: cũng dùng mock placeholder; format-invalid đầu ra sẽ được ghi nhận, không gọi lại.
PYTHONPATH=src python3 -m table_qa.main \
  --config config/default.yaml \
  --input-mode raw --answer-mode direct

# Benchmark Gemini / OpenRouter; CLI tự nạp key từ .env ở thư mục gốc:
PYTHONPATH=src python3 -m table_qa.main --config config/benchmark_gemini.yaml
PYTHONPATH=src python3 -m table_qa.main --config config/benchmark_openrouter.yaml

# Competition profile từ chối provider/model không đúng trước khi đọc dữ liệu:
PYTHONPATH=src python3 -m table_qa.main --config config/submission.yaml

# Tạo ZIP source-only dưới 100 MB; preflight 20 query và 10 workbook.
PYTHONPATH=src python3 tools/package_submission.py
```

Competition khóa input ở `data/Public data 1/queries.json` và
`data/Public data 1/xlsx_files/`, dùng `raw/plan_tools/planner_v1`, yêu cầu đủ 20
query và 10 workbook, và từ chối mọi CLI override cùng nhãn đánh giá. Mỗi run đo high-water RSS
trong process, chặn phát hành CSV khi chạm `max_rss_mb`, đồng thời ghi RSS vào
`summary.json`. ZIP không chứa query, workbook hay `sample_submission.csv`; scoring
environment cần mount data tại đúng các path trong config. Xem
[`SUBMISSION_README.md`](SUBMISSION_README.md) để chạy gói đã giải nén.

## Các phần đã triển khai

| File/thư mục | Nội dung |
|---|---|
| `src/table_qa/contracts.py` | Dataclass cho query, bảng raw/parsed, context, answer spec, kết quả/evidence/trace, lỗi, LLM request/response/usage, runtime limits; TypedDict cho plan |
| `src/table_qa/interfaces.py` | Protocol cho adapter, context builder, LLM client, validator, tool, executor, formatter và evaluator |
| `src/table_qa/contract_validation.py` | Đọc schema local; kiểm tra shape, provenance, hash, refs/dependencies, selector theo mode và các giới hạn cấu hình |
| `src/table_qa/query_loader.py` | Đọc query theo thứ tự file, kiểm tra ID, workbook và containment đường dẫn/symlink |
| `src/table_qa/adapters/raw.py` | XLSX reader standard library, giữ cell tọa độ/giá trị/format/ngày/merged ranges, serialize toàn bộ sheet |
| `src/table_qa/adapters/parsed.py` | Đọc `table_context_v1` JSON; có thể xác minh hash workbook gốc |
| `src/table_qa/context_builder.py` | Suy ra AnswerSpec bằng luật cục bộ và ghép QueryContext chứa full-table view |
| `src/table_qa/runner.py` | Cache bảng/view, routing, tối đa một plan repair, format/validate đáp án, artifacts, metrics, label evaluation và fail-run CSV policy |
| `src/table_qa/answer_spec.py`, `validator.py`, `planner.py`, `direct.py`, `prompts/` | Prompt versioned, strict JSON parsing (cấm duplicate keys/NaN), formatter và validator cho tám AnswerSpec |
| `src/table_qa/execution/` | Resolver giữ refs/evidence/unit lineage; registry thực thi 16 operator; executor áp limits và ghi trace/artifact từng step |
| `src/table_qa/config.py`, `config/*.yaml` | Config strict, env-key names, benchmark profiles và competition lock |
| `src/table_qa/llm/` | Factory, OpenRouter/Gemini adapters, error mapping, usage normalization và latency |
| `src/table_qa/logging.py`, `budget.py` | JSONL event log và tổng hợp usage/cost; cost không rõ giữ null |
| `src/table_qa/main.py`, `mock_llm.py` | CLI đa provider, mock offline mặc định và JSON mock phụ thuộc request/mode |
| `src/table_qa/benchmark.py` | Chạy E1/E2, tùy chọn A/B trên dev; chỉ bật E3/E4 khi parser marker, hash và toàn bộ cell source qua preflight; lưu manifest và artifacts từng run |
| `src/table_qa/evaluate.py` | Bảng Format Valid/Exact Match và trace query, kèm token/cost/elapsed |
| `src/table_qa/synthetic.py`, `prompts/planner_v1_b.txt` | Split synthetic dev/holdout theo workbook và biến thể prompt B một thay đổi |
| `tools/package_submission.py`, `run_submission.sh`, `requirements-submission.txt` | Preflight input, ZIP allowlist dưới 100 MB, entrypoint và dependency manifest cho scoring |
| `schemas/plan_v1.json` | Envelope và schema args đóng cho đủ 16 operator; không chấp nhận code hoặc key tùy ý |
| `schemas/table_context_v1.json` | Cell/row/fact/section/sheet và envelope bảng versioned |
| `schemas/answer_spec_v1.json` | Tám dạng đáp án, precision, separator, cardinality, allowed values |
| `schemas/query_v1.json`, `query_context_v1.json`, `table_view_v1.json` | Input query, context và full-table view |
| `schemas/runtime_limits_v1.json` | Một HTTP attempt/call; tối đa một call sửa plan; giới hạn steps/records/cells/deadline |
| `tests/fixtures/q006/` | Bảng raw/parsed, query/context, hai plan hợp lệ và nhãn/evidence tham chiếu riêng |
| `tests/fixture_plans.py`, `tests/test_m3_pipeline.py` | 20 plan kiểm thử offline; end-to-end answer/CSV, format, repair, no-retry và label isolation |
| `tests/test_execution_engine.py` | Kiểm tra Q006 raw/parsed chạy thật, evidence, derived absolute, kiểu so sánh và CLI báo lỗi |

Kiểm tra M2 ngày 2026-10-15: 36 tests pass; Q006 raw và parsed đều tính ra
`7668` với evidence `E25/E26`; plan ví dụ Q001 chạy qua `select_records`,
`derive`, `sort`, `take`, `project`. Tại thời điểm M2, `direct` và validator còn
stub, chưa có fixture plan thực cho đủ 20 câu, và runner chưa cưỡng chế deadline.
M3 bổ sung fixture 20 câu và deadline query/run; executor áp dụng giới hạn
step/cell/record.

M3 đã được triển khai ở mức thành phần và luồng: cả tám answer format có formatter
kiểu dữ liệu; DIRECT gọi đúng một lần; PLAN+TOOLS chỉ sửa một lần cho lỗi JSON,
schema hoặc lỗi plan/execution được cho phép; API/format errors không được repair.
Kiểm tra đầu cuối với Q006 fixture plan tạo `7668` và evidence `E25/E26`, đồng
thời ghi CSV. Hai mươi fixture plan chạy qua raw pipeline đều format-valid, khớp
nhãn public 20/20 và tạo CSV; nhãn chỉ được đọc ở bước evaluation, không đưa vào
prompt. Bộ kiểm tra cũng xác nhận lỗi format/API không repair và CSV quote đúng
nhãn có dấu phẩy. Mock mặc định cố ý không sinh đáp án benchmark.

M4 nối runner/CLI với contract `LLMClient`. OpenRouter dùng OpenAI-compatible SDK,
Gemini dùng `google-genai`; cả hai áp request timeout từ runner, tắt SDK retry,
chuẩn hóa usage/model/request ID/latency và không ghi API key. Gemini tắt
automatic continuation để một lần gọi không tạo thêm request. Factory fail-fast
nếu thiếu key; profile `competition` chỉ nhận
`openrouter/qwen/qwen3.7-flash`. Runner hiện chạy tuần tự nên chỉ nhận
`max_workers: 1`; giá trị concurrency lớn hơn bị từ chối thay vì bị bỏ qua.

Runner mặc định thử tối đa 3 HTTP attempt. Lỗi server/timeout dùng backoff 0,5
giây rồi 1 giây; rate-limit ưu tiên `Retry-After`, nếu thiếu thì chờ 10 giây rồi
30 giây. Từng attempt được ghi riêng trong log. Bộ
lọc loại record có field `null` khỏi các predicate thông thường và lỗi
`MISSING_VALUE` còn lại có thể kích hoạt một lần sửa plan.
M4 được kiểm tra bằng fake transport gồm Q006 đi từ adapter đến `submission.csv`;
chưa thực hiện request API tính phí.

Dataclass là container typed, constructor không tự kiểm tra mọi invariant.
Ở boundary, gọi `validate_table_context`, `validate_answer_spec`,
`validate_query_context` hoặc `validate_plan` trước khi dùng dữ liệu.
JSON Schema kiểm tra shape; checks liên field như ref tới step trước,
sheet/cell/index khớp nhau và hash text được thực hiện bằng Python.
Kiểu kết quả thực tế của từng tool, giới hạn tài nguyên khi thực thi và
tính đúng của phép toán được kiểm tra ở M2, không thể xác nhận chỉ bằng schema.

## Ví dụ dùng contract

```python
import json
from pathlib import Path
from table_qa.contract_validation import (
    validate_query_context, validate_table_context, validate_plan,
)
from table_qa.execution import execute_plan

fixture = Path("tests/fixtures/q006")
table = validate_table_context(json.loads((fixture / "table_raw.json").read_text()))
context = validate_query_context(json.loads((fixture / "query_context_raw.json").read_text()))
plan = validate_plan(json.loads((fixture / "plan_raw.json").read_text()), context)
# M0 xác nhận contract; M2 cung cấp execute_plan cho các plan đã validate.
execution = execute_plan(plan, table)
print(execution.value)  # 7668
```

Các schema dùng JSON Schema Draft 2020-12, cấm extra keys trừ các dict metadata
được chỉ định mở. Tham chiếu schema chỉ được resolve từ registry local, không tải
qua mạng. `$id` là định danh schema, không phải dịch vụ cần gọi.
JSON Schema `default` chỉ là tài liệu; DTO bổ sung default cho các field optional
được bỏ qua. Plan validator không sửa logic hoặc tự thêm default vào plan.

## Quyết định đã chốt

- DIRECT và PLAN+TOOLS nhận toàn bộ bảng của cùng workbook. Context không bị cắt;
  hash xác nhận text không đổi, nhưng tự nó không chứng minh serializer đã giữ đủ
  nguồn. Fixture test đối chiếu coverage với workbook public khi file nguồn có sẵn.
- Raw để LLM hiểu bảng và chọn tọa độ; không đòi raw resolver suy luận header.
  Parsed hỗ trợ thêm selector có full section/row/column paths.
- Không retry API/SDK. `max_planner_repair_calls` chỉ là 0 hoặc 1, mặc định 1.
  Repair chỉ dựa lỗi plan quan sát được, không dựa nhãn. Runner M3 thực hiện
  chính sách này; lỗi API và format không được sửa.
- `StepResult` giữ value và evidence; `ExecutionResult` thêm trace. Evidence là
  lineage bảo thủ, không cam kết tập ô tối thiểu.
- Runtime `Decimal` và `date` ghi artifact bằng tag
  `{"$type":"decimal","value":"40.60"}` và
  `{"$type":"date","value":"2024-02-29"}` để round-trip không mất precision/kiểu.
  Đây là biểu diễn artifact, không phải dạng operand model cần sinh trong plan.
  `$type` được dành riêng trong runtime record để không nhầm record với tag.
- Chỉ đánh giá format và Exact Match khi có nhãn. Token/cost/elapsed là telemetry;
  dữ liệu chưa biết để null. Không có metric accuracy LLM từ fixture.

## Điều kiện hoàn thành M0

- [x] DTO, Protocol/signatures và schema version 1 có trong repo.
- [x] Schema và các kiểm tra contract có test offline cho cả dữ liệu hợp lệ và lỗi.
- [x] Fixture Q006 raw và parsed cùng trỏ tới `cpsaat07!E25/E26`, giá trị
  `16796/9128`, đáp án tham chiếu `7668`; section Women ở A24.
- [x] Parsed fixture được ghi rõ là thủ công, `parsed_integrated=false`.

M1 trước đó xác nhận routing trên 20 raw query và parsed fixture Q006. M2 thay
executor stub bằng tool engine thật. M3 thêm answer validation và CSV; output run
gồm `logs.jsonl`, `results.jsonl`, `summary.json`, `executions.jsonl`,
`step_results.jsonl` và `submission.csv` khi mọi query hợp lệ trong
`runs/<run_id>/`.

Raw XLSX reader dùng standard library, không giải nén workbook ra filesystem,
giữ tất cả worksheet và merged ranges, cached formula values, kiểu dữ liệu và
các number/date format phổ biến. Excel render format phức tạp và công thức thiếu
cached value chưa được tái tính ở M1; các mốc sau cần báo rõ khi một giá trị
không đọc hoặc chuyển đổi được.
