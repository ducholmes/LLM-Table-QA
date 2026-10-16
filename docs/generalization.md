# Ke hoach tong quat hoa bai thi cho private data

## Trang thai trien khai (2026-10-16)

Da trien khai cac hang muc cot loi cua Phase 1-3:

- CLI portable `--input-dir`, `--workbook-dir`, `--output`, `--artifacts-dir`.
- Submission profile khong con khoa path public hoac so luong 20/10.
- Path resolver an toan, ho tro path co space/Unicode va output dich chinh xac.
- Query loader cho phep metadata bo sung nhung chi dua ba field contract vao pipeline;
  ID khong con bi gioi han o ASCII va JSON trung key bi tu choi.
- `generic_string` fallback cho answer format chua biet.
- `emit_all_rows`: query loi van co mot dong voi answer rong.
- CSV atomic, kiem tra ID unique va kieu answer truoc khi commit.
- ZIP builder khong can public data; entrypoint nhan input/output runtime.
- README submission va black-box tests da cap nhat.

Da xac minh bang 83 unit/integration tests va mot package ZIP build trong thu muc
tam. Phase 4 van la cong viec benchmark/robustness lien tuc khi co them private-like
fixtures; khong co cach dam bao accuracy cho moi workbook shape chi bang I/O refactor.

## 1. Muc tieu va contract can dam bao

Chuong trinh phai hoat dong nhu mot ung dung dong goi doc lap:

```text
thu muc input
  |- queries.json
  `- cac workbook .xlsx

        |  mot lenh CLI
        v

submission.csv
```

Contract toi thieu:

1. Nhan mot thu muc input moi, khong phu thuoc ten/path bo public.
2. Tu tim `queries.json` va cac workbook duoc tham chieu trong query.
3. Moi `id` hop le trong `queries.json` xuat hien dung mot lan trong output.
4. `answer` chi chua dap an cuoi cung, khong co giai thich, markdown hay don vi
   neu cau hoi khong yeu cau.
5. Ghi dung file `submission.csv` tai path nguoi chay chi dinh.
6. Package chua source, config, dependency manifest va README; khong kem data,
   label, API key hay run artifact.
7. Co the cai dat va chay bang command line trong moi truong sach.

Tai lieu nay coi cac con so va duong dan cua bo public la fixture de test, khong
phai invariant cua bai toan.

## 2. Cac rang buoc hard-code hien tai can loai bo

Hien tai source co cac diem khong tong quat sau:

| Vi tri | Rang buoc hien tai | Anh huong tren private data |
|---|---|---|
| `config.py` | competition bat buoc `data/Public data 1/...` | Giam khao dung thu muc khac se bi tu choi |
| `config.py` | bat buoc 20 query va 10 workbook | Dataset co kich thuoc khac se bi tu choi |
| `main.py` | competition cam override `--queries`, `--data-dir`, `--run-root` | Khong the gan input/output runtime |
| `config/submission.yaml` | path va so luong public co dinh | Goi submission khong portable |
| `run_submission.sh` | khong nhan argument | Giam khao phai mount dung layout cua doi |
| `package_submission.py` | build ZIP phai doc bo public va preflight 20/10 | Khong build duoc khi chi co source |
| `SUBMISSION_README.md` | huong dan mount path public co dinh | Khong dung contract cham tong quat |
| `runner.py` | `submission.csv` nam trong run directory ngau nhien | Kho tim output neu he thong cham can mot path cu the |
| `runner.py` | mot query loi thi khong tao CSV | Vi pham yeu cau moi ID co mot dong ket qua |
| `context_builder.py` | chi nhan dien mot tap answer format han che | Cau hoi private co cach dien dat moi de bi reject |
| `query_v1.json` | `additionalProperties: false` | Private query co metadata bo sung co the bi reject |

Can sua cac diem tren truoc khi toi uu accuracy hoac token.

## 3. Thiet ke CLI tong quat

## 3.1. Giao dien chinh de xuat

Cung cap mot lenh don gian dung dung ngon ngu cua de bai:

```bash
python -m table_qa.main \
  --input-dir /path/to/private_input \
  --output /path/to/submission.csv \
  --config config/submission.yaml
```

Quy uoc:

- `--input-dir` bat buoc trong che do submission.
- `queries.json` mac dinh la `<input-dir>/queries.json`.
- Workbook root mac dinh la chinh `input-dir`.
- `--output` la file CSV dich chinh xac, khong phai thu muc run.
- Log/artifact co the ghi vao `--artifacts-dir`; neu khong truyen thi dung mot
  thu muc tam hoac `<output-parent>/artifacts`.

Van co the giu interface cap thap cho development:

```bash
python -m table_qa.main \
  --queries /path/queries.json \
  --data-dir /path/xlsx_files \
  --output /path/submission.csv
```

Rule precedence phai ro rang:

```text
CLI explicit > environment > config file > default
```

Khong nen de competition profile cam override input/output. Profile chi nen khoa
nhung thong so thuat toan da chot, vi du provider, model, prompt version va limit.

## 3.2. Resolve path

Thuc hien mot ham duy nhat:

```python
@dataclass(frozen=True)
class InputLayout:
    input_dir: Path
    queries_path: Path
    workbook_root: Path
    output_path: Path
    artifacts_dir: Path | None
```

`resolve_input_layout()` can:

1. Resolve absolute path mot lan.
2. Kiem tra input directory ton tai va la directory.
3. Kiem tra `queries.json` la regular file.
4. Dam bao moi `file_name` resolve nam trong workbook root, ke ca symlink.
5. Khong phu thuoc current working directory.
6. Chap nhan path co space va Unicode.
7. Tao parent cua output neu can, nhung khong tu tao input.
8. Tu choi output trung input file/workbook hoac nam trong source package neu co
   nguy co ghi de du lieu.

Neu official layout dat workbook trong subdirectory, co the ho tro:

```bash
--input-dir /data/private --workbook-dir /data/private/xlsx_files
```

nhung default van phai dung duoc voi mot thu muc duy nhat.

## 4. Tong quat hoa input contract

## 4.1. `queries.json`

Moi record can toi thieu:

```json
{
  "id": "unique-id",
  "file_name": "table.xlsx",
  "query": "question text"
}
```

Validation bat buoc:

- Top-level la array va khong rong.
- `id`, `file_name`, `query` la chuoi khong rong.
- `id` unique sau khi doc toan bo file.
- `file_name` tham chieu file `.xlsx` ton tai ben trong workbook root.
- Khong chap nhan absolute path, `..`, symlink escape hoac extension khac.
- Giu nguyen thu tu query de ghi output.

Chinh sach voi extra fields can cau hinh ro. Khuyen nghi private-compatible:

- Cho phep metadata bo sung va bo qua no sau khi log danh sach key.
- Van cam key trung lap trong JSON.
- Khong dua metadata khong ro nguon vao prompt.

Co the giu strict schema trong test noi bo, nhung submission parser chi enforce
required fields. Cach nay tranh fail neu ban to chuc them metadata vao private set.

## 4.2. Workbook discovery

Khong scan va bat buoc xu ly moi `.xlsx` trong directory. Chi load workbook duoc
tham chieu boi query; cac file phu khong lien quan khong duoc lam run fail.

Can ho tro:

- So query bat ky >= 1.
- So workbook bat ky >= 1.
- Mot hoac nhieu query dung chung workbook.
- Query xen ke giua cac workbook.
- Sheet name va file name co Unicode/space.
- Nhieu sheet, hidden sheet, merged cells, blank rows/columns, formula cached value,
  date/time, percentage, number format va text dai.

Khong hard-code ten workbook, sheet, range, header row, so cot/hang hoac ngon ngu
cua noi dung.

## 4.3. Gioi han va file loi

Truoc run, preflight theo input thuc te:

- File ton tai, dung ZIP/XLSX signature va doc duoc.
- Khong encrypted/password-protected neu adapter khong ho tro.
- Tong so workbook/query/cell va kich thuoc file nam trong resource limit.
- Loi ghi ro `query_id`, `file_name`, stage va error code; khong de stack trace la
  output chinh cho giam khao.

`expected_query_count` va `expected_workbook_count` chi nen la tuy chon debug.
Trong submission config, dat `null` hoac bo han hai field nay.

## 5. Tong quat hoa xu ly cau hoi va dap an

## 5.1. Khong phu thuoc 20 query public

Prompt, planner va executor khong duoc chua mapping query ID -> plan/dap an. Query
ID chi la identity de doi chieu output, khong mang y nghia nghiep vu.

Can co test bao ve:

- Doi toan bo query ID nhung giu cau hoi/table thi pipeline van tao cung answer.
- Them query moi voi ID chua tung thay thi khong bi reject.
- Nhung file nhu labels/sample submission khong bao gio duoc doc trong submission
  mode.

## 5.2. Answer format

`parse_answer_spec()` hien suy ra format bang mot tap regex va co the nem
`ANSWER_SPEC_UNSUPPORTED`. Private question co cach dien dat moi se gay fail truoc
khi goi model.

De xuat hai lop:

1. **Explicit format rules** cho cac format da biet: integer, fixed decimal,
   yes/no, ordered labels, last name...
2. **Generic final-answer fallback** khi khong match: mot chuoi khong rong, da trim,
   mot dong, khong explanation/markdown. LLM nhan huong dan format truc tiep tu
   question va local validator chi enforce envelope + safety.

Them `AnswerSpec(kind="generic_string")` vao contract/schema/formatter. Fallback
khong duoc tu dong bien `null` thanh text. Neu question yeu cau nhieu gia tri,
giu separator dung nhu question neu co the suy ra.

## 5.3. Plan/tool generality

16 operator hien tai phu hop nhieu query bang, nhung can benchmark private-like:

- Lookup theo cell va semantic header.
- Filter theo text/number/date/null.
- Sum/average/min/max/count.
- Difference, ratio, percent change va absolute change.
- Ranking, tie va top-k.
- Multi-row header, merged header, subtotal va units.
- Cross-sheet va nhieu range trong cung workbook.

Neu tool catalog khong bieu dat duoc mot nhom query, them operator tong quat co
schema dong thay vi them logic gan query ID.

## 6. Output contract va failure policy

## 6.1. `submission.csv`

File cuoi phai co dung header:

```csv
id,answer
```

Va cac invariant:

- So data row bang so query record hop le.
- Moi ID input xuat hien dung mot lan.
- Khong co ID la.
- Thu tu output giong `queries.json`, tru khi de mau quy dinh khac.
- Dung UTF-8, `newline=""` va `csv.DictWriter` de quote dau phay/newline/dau nhay.
- `answer` la scalar string; khong ghi JSON object, explanation hoac evidence.
- Ghi atomic: tao temp file trong cung filesystem, flush/fsync neu can, sau do
  replace output; khong de lai CSV dang do khi process bi dung.

Them `output_path` vao `run_offline()` hoac tach mot `SubmissionWriter` ra khoi
runner. `run_dir/submission.csv` co the giu nhu artifact phu, nhung CLI phai dam
bao file tai `--output`.

## 6.2. Khi mot query loi

Yeu cau “moi id dung mot dong” xung dot voi policy hien tai “mot query loi thi
khong tao CSV”. Can chot policy tong quat:

- **Khuyen nghi cho submission:** luon ghi du moi ID; query loi co `answer=""`.
- Ghi chi tiet loi vao `errors.jsonl`/stderr, khong dua loi vao `answer`.
- Exit code co the la non-zero de bao partial failure, nhung CSV van ton tai de he
  thong cham co artifact hop le.
- Co `--fail-fast` cho development, khong bat trong submission.

Neu quy che cam blank answer, fallback cuoi co the la mot direct LLM call; neu van
loi thi ghi blank van tot hon mat ca file. Chinh sach nay phai duoc ghi ro trong
README va test.

## 7. Config va profile moi

`config/submission.yaml` khong chua dataset-specific path/count:

```yaml
run_profile: submission
input_mode: raw
answer_mode: plan_tools
prompt_version: planner_v1
schema_version: plan_v1
context_policy: full_table
submission_policy: emit_all_rows
max_workers: 1

llm:
  provider: openrouter
  model: qwen/qwen3.7-flash
  json_mode: json_schema
  max_output_tokens: 2400
  request_timeout_seconds: 90
  max_api_attempts: 3
  max_planner_repair_calls: 1
  provider_options:
    openrouter:
      api_key_env: OPENROUTER_API_KEY
      base_url: https://openrouter.ai/api/v1
      temperature: 0

query_timeout_seconds: 240
run_timeout_seconds: 2400
max_rss_mb: 1024
```

Input/output la runtime data nen truyen qua CLI, khong dong bang trong YAML.

Sua validation:

- Bo `competition` check fixed path.
- Bo check `expected_query_count == 20` va `expected_workbook_count == 10`.
- Cho phep CLI override input/output/artifact path trong submission profile.
- Van chan override provider/model/prompt neu muon dong bang bai nop.
- Validation error phai noi ro field va gia tri, khong in secret.

## 8. Refactor kien truc de nghi

Tach pipeline thanh cac boundary ro rang:

```text
CLI
  -> InputLayoutResolver
  -> QueryManifestLoader
  -> WorkbookRepository (cache theo resolved path/hash)
  -> QueryProcessor
       -> ContextBuilder
       -> LLM/Planner
       -> PlanValidator + Executor
       -> AnswerFormatter
  -> SubmissionWriter
  -> ArtifactReporter
```

### `InputLayoutResolver`

Chi xu ly argument, path, containment va output destination.

### `QueryManifestLoader`

Doc/validate JSON, giu order va tao danh sach `QueryJob`:

```python
@dataclass(frozen=True)
class QueryJob:
    input_index: int
    query: Query
    workbook_path: Path
```

### `WorkbookRepository`

Load moi workbook mot lan, cache table/view, khong biet query public/private.

### `QueryProcessor`

Nhan mot `QueryJob`, tra `QueryOutcome`; khong ghi file truc tiep. Sau nay co the
group/batch query theo workbook ma van khoi phuc order bang `input_index`.

### `SubmissionWriter`

La boundary duy nhat tao CSV; validate bijection ID truoc khi atomic commit.

### `ArtifactReporter`

Ghi log/summary vao noi rieng. Loi artifact khong duoc lam hong `submission.csv`
da tao hop le, tru khi resource/safety policy yeu cau.

## 9. Dong goi ZIP tong quat

## 9.1. Sua `package_submission.py`

Build source ZIP khong duoc phu thuoc public dataset. Thay `_validate_source_data()`
bang cac check:

- Config submission parse duoc va khong co fixed input path/count.
- Entry point ton tai va executable hoac chay duoc bang `sh`.
- Source, prompt va schema package day du.
- README va dependency manifest co trong ZIP.
- Khong chua `.env`, API key, data, labels, tests cache, runs, `.git`, virtualenv.
- ZIP duoi size limit.

Neu can smoke test, tool tu tao mot dataset XLSX toi thieu trong temporary directory
hoac dung fixture nho duoc phep dong goi; khong doc public dataset.

## 9.2. `run_submission.sh`

Cho phep argument va quote path dung:

```bash
sh run_submission.sh /path/to/input /path/to/submission.csv
```

Script can:

- `set -eu`.
- Resolve package root theo vi tri script, khong theo CWD.
- Kiem tra dung 2 argument hoac ho tro flag ro rang.
- Goi module voi `--input-dir "$1" --output "$2"`.
- Khong cai dependency, download data hay ghi vao input directory.
- Tra exit code cua Python process.

## 9.3. Dependency va moi truong

- Pin compatible ranges hoac lock versions da test.
- Ghi Python version toi thieu.
- Neu can network de goi API, README noi ro provider endpoint va env var.
- Khong yeu cau editable install neu giam khao chi giai nen ZIP; `PYTHONPATH=src`
  phai chay duoc.
- Khong phu thuoc package/system command khong khai bao.
- `.env` chi la tien ich local; production uu tien environment variable.

## 10. README danh cho giam khao

README trong ZIP nen ngan va co the copy/paste:

```bash
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install -r requirements-submission.txt
export OPENROUTER_API_KEY='...'
sh run_submission.sh /absolute/path/to/private_input /absolute/path/to/submission.csv
```

README phai neu:

- Cau truc input du kien.
- Lenh cai va chay duy nhat.
- Env var/API credential can thiet.
- Vi tri output chinh xac.
- Python/OS da test.
- Timeout, network va resource assumptions.
- Y nghia exit code.
- Khong de huong dan public-only trong submission README.

## 11. Bao mat va cach ly private data

- Khong log API key, Authorization header hoac full environment.
- Khong ghi table/query vao output CSV.
- Log full prompt/raw response nen tat mac dinh trong submission hoac can co flag
  debug, vi private data co the nhay cam.
- Khong upload workbook den dich vu nao ngoai provider da cong bo trong README.
- Khong doc file ngoai input root/config/package root.
- Khong follow symlink thoat input root.
- Khong tai code/model tuy y luc runtime.
- Label/sample submission neu co trong directory khong duoc dung de suy ra answer.

## 12. Test tong quat hoa bat buoc

## 12.1. CLI black-box tests

Chay process tu mot CWD bat ky voi temporary directory:

1. Input co 1 query/1 workbook.
2. Input co so query/workbook khac 20/10.
3. Path co space va Unicode.
4. Query xen ke giua 2 workbook.
5. Co workbook phu khong duoc tham chieu.
6. Output parent chua ton tai.
7. Run lai voi output da ton tai; replace atomic.
8. Missing `queries.json`, missing workbook, malformed JSON/XLSX.
9. Duplicate ID va path traversal.
10. Mot query loi nhung CSV van co moi ID theo policy.

Moi test kiem tra:

```text
header == ["id", "answer"]
row_count == query_count
Counter(output_ids) == Counter(input_ids)
answer khong chua explanation envelope
```

## 12.2. Data-shape tests

- Header o row khac row 1.
- Multi-row va merged headers.
- Blank row/column o dau va giua table.
- Nhieu table/section trong mot sheet.
- Formula co/khong co cached value.
- Date serial, percentage, negative, decimal va thousands separator.
- Non-English/Unicode content.
- Hidden sheet va merged range.
- Table lon sat resource limit.

## 12.3. Packaging test

CI nen:

1. Build ZIP ma khong co thu muc public data.
2. Giai nen vao temp directory moi.
3. Cai dependency tu manifest.
4. Tao/mount synthetic private-like input o path bat ky.
5. Chay dung lenh trong README.
6. Validate output CSV.
7. Scan ZIP de chac chan khong co secret/data/artifact.

Day la test quan trong nhat de mo phong cach ban giam khao chay bai.

## 13. Thu tu trien khai de xuat

### Phase 1 - Portable I/O contract

1. Them `--input-dir`, `--output`, `--artifacts-dir`.
2. Them `InputLayoutResolver` va path containment.
3. Truyen `output_path` xuong submission writer.
4. Bo fixed paths/counts va cho override input/output trong submission profile.
5. Cap nhat `run_submission.sh`.

**Done khi:** chay duoc cung mot ZIP tren hai temp input directory khac nhau ma
khong sua config/source.

### Phase 2 - Output completeness va generic answer

1. Tach `SubmissionWriter` va validate ID bijection.
2. Doi policy thanh `emit_all_rows`.
3. Them `generic_string` answer fallback.
4. Bao toan input order ke ca khi group query theo workbook.

**Done khi:** moi input ID luon co dung mot output row, ke ca partial failure.

### Phase 3 - Package doc lap

1. Bo public-data preflight khoi package builder.
2. Viet lai `SUBMISSION_README.md` theo lenh tong quat.
3. Kiem tra dependency/source/schema/prompt trong ZIP.
4. Them unpacked-package smoke test.

**Done khi:** may/moi truong sach chi can ZIP, input, credential va cac lenh trong
README de tao CSV.

### Phase 4 - Robustness tren private-like data

1. Them matrix workbook shape va question format.
2. Audit raw adapter/plan operators theo cac case fail.
3. Them fallback/failure isolation.
4. Benchmark accuracy, token, latency va memory tren synthetic holdout chua tung
   dung de chinh prompt.

**Done khi:** khong con assumption gan voi ID/path/count/layout public trong
production source.

## 14. Acceptance checklist cuoi

- [ ] Khong co chuoi `Public data 1` trong production config/source/README submission.
- [ ] Khong co invariant `20 queries` hoac `10 workbooks` trong submission path.
- [ ] `run_submission.sh <input-dir> <output.csv>` chay tu CWD bat ky.
- [ ] Chi workbook duoc queries tham chieu moi duoc load.
- [ ] Moi query ID co dung mot dong output.
- [ ] CSV chi co `id,answer` va answer khong kem giai thich.
- [ ] Output duoc ghi atomic tai path do giam khao chi dinh.
- [ ] Package build duoc khi khong co public dataset.
- [ ] ZIP khong chua data, label, secret, run artifact hay cache.
- [ ] README cai/chay duoc trong moi truong sach.
- [ ] Tests bao phu path Unicode/space, so luong bat ky, malformed input va partial failure.
- [ ] Khong co prompt/fixture mapping theo public query ID.
- [ ] Submission mode khong doc label/sample answer.
- [ ] Resource limit va timeout tinh theo input thuc, khong theo kich thuoc public.

## 15. Pham vi cua tai lieu

Day la ke hoach trien khai, chua phai thay doi source. Uu tien dau tien la sua
contract input/output va package portability; toi uu batch/token chi nen lam sau
khi black-box private-data run da on dinh, va khong duoc lam thay doi thu tu hay
lam mat ID trong `submission.csv`.
