# Phuong huong toi uu input token khi nhieu query dung chung workbook

## 1. Hien trang

Trong mot lan chay `run_offline`, workbook da duoc cache o local bang:

- `table_cache`: cache `TableContext` sau khi doc/parse workbook.
- `view_cache`: cache chuoi `table_view` sau khi render.

Cache nay chi giam I/O va chi phi parse local. Voi moi query, pipeline van:

1. Tao mot `QueryContext` moi.
2. Ghep toan bo `context.table_view` vao `user_prompt`.
3. Gui mot API request moi den LLM.

Vi vay, neu mot workbook co `T` input tokens va co `Q` query, phan table hien tai
duoc dua vao request xap xi `Q * T` tokens. Plan repair va HTTP retry cung co the
gui lai cung noi dung nay.

Code lien quan:

- `src/table_qa/runner.py`: khoi tao `table_cache`, `view_cache` va lap tung query.
- `src/table_qa/planner.py`: `_base_user_prompt()` ghep `TABLE_CONTEXT` vao moi prompt.
- `src/table_qa/direct.py`: direct prompt cung ghep lai toan bo `TABLE_CONTEXT`.

## 2. Can phan biet hai muc tieu

### 2.1. Giam token thuc su trong request/context

Can khong dua lai toan bo table vao moi lan suy luan. Cac cach thuoc nhom nay la:

- Gom nhieu query cua cung workbook vao mot request.
- Chi gui mot phan table lien quan den query.
- Cho LLM tra cuu table qua tool thay vi nhan full table.

Day la cach giam context length, payload mang va thuong ca latency.

### 2.2. Giam chi phi bang provider prompt/context caching

Provider van coi cached content la context cua model va usage co the van bao tong
`prompt_tokens`, nhung tach them `cached_prompt_tokens` de tinh gia thap hon. Cach
nay chu yeu giam chi phi va co the giam latency; no khong lam context window nho di.

Khong nen dung `prompt_tokens_total` mot minh de danh gia thanh cong. Can theo doi:

```text
uncached_prompt_tokens = prompt_tokens - cached_prompt_tokens
cache_hit_rate = cached_prompt_tokens / prompt_tokens
effective_input_cost_usd
```

## 3. Cac phuong an

## Phuong an A - Sap xep prompt de tan dung implicit prefix caching

### Y tuong

Prompt caching tu dong thuong cache mot prefix giong nhau giua cac request. Hien
tai phan dau user prompt chua `QUERY_ID`, cau hoi va answer spec; nhung truong nay
thay doi theo query. `TABLE_CONTEXT` nam sau chung, nen prefix bi khac truoc khi
provider den duoc phan table.

Can doi thu tu thanh:

```text
SYSTEM PROMPT (co dinh)
INPUT_MODE (co dinh trong run)
TABLE_ID (co dinh theo workbook)
TABLE_CONTEXT (co dinh theo workbook)
--- DYNAMIC QUERY ---
QUERY_ID
QUESTION_JSON
EXPECTED_ANSWER_SPEC
ANSWER_MODE
```

Moi query tren cung workbook se co prefix lon giong het nhau, con du lieu dong nam
o cuoi prompt.

### Trien khai

1. Tach prompt thanh hai ham trong `planner.py` va `direct.py`:

   - `build_static_context(context)`: mode, table ID va table view.
   - `build_dynamic_query(context)`: query ID, question va answer spec.

2. Ghep static context truoc dynamic query trong ca `plan_tools` va `direct`.
3. Dam bao serialization cua table hoan toan deterministic; khong chen timestamp,
   run ID hoac query ID vao static prefix.
4. Sap xep xu ly cac query theo workbook de cac request cung prefix duoc gui lien
   tiep, dac biet khi cache co TTL ngan. Van luu `original_query_index` va sap xep
   ket qua/submission ve thu tu ban dau.
5. Voi OpenRouter, them `session_id` hoac `prompt_cache_key` on dinh theo
   `(model, prompt_version, input_mode, table_view_hash)` neu model/provider ho tro.
6. Them test xac nhan hai query cung workbook co chung prefix den het table va chi
   khac o dynamic suffix.

### Uu/nhuoc diem

- Thay doi nho, it rui ro, khong doi contract output.
- Phu hop lam buoc dau tien.
- Cache hit khong duoc dam bao va co nguong token toi thieu tuy model.
- Chu yeu giam chi phi cached input, khong giam context length.

## Phuong an B - Explicit context cache theo workbook

### Y tuong

Tao mot cache provider cho tung workbook, chua system instruction va table view.
Moi query sau do chi gui dynamic query va tham chieu `cached_content`/cache key.

Voi Gemini `generateContent`, flow mong muon la:

```text
prepare workbook
    -> caches.create(model, system_instruction, table_context, ttl)
    -> cache_name

query 1 -> generate_content(contents=dynamic_query_1,
                            config.cached_content=cache_name)
query 2 -> generate_content(contents=dynamic_query_2,
                            config.cached_content=cache_name)
...
finally -> caches.delete(cache_name), hoac de TTL tu het han
```

OpenRouter co the dung explicit `cache_control` breakpoint trong content block voi
model/provider ho tro. Khong nen mac dinh moi model OpenRouter deu ho tro cung mot
co che cache.

### Thay doi kien truc de nghi

Khong chen logic cache truc tiep vao `runner.py` theo tung provider. Mo rong lop
LLM bang mot capability tuy chon:

```python
class ContextCacheClient(Protocol):
    def prepare_context(self, spec: ContextCacheSpec) -> PreparedContext: ...
    def generate_with_context(
        self, request: LLMRequest, prepared: PreparedContext
    ) -> LLMResponse: ...
    def release_context(self, prepared: PreparedContext) -> None: ...
```

DTO de nghi:

```python
@dataclass(frozen=True)
class ContextCacheSpec:
    key: str                 # table_view_hash + model + prompt version + mode
    system_prompt: str
    static_context: str
    ttl_seconds: int

@dataclass(frozen=True)
class PreparedContext:
    provider: str
    key: str
    remote_name: str | None
```

Runner giu:

```python
prepared_contexts: dict[tuple[str, str, str, str], PreparedContext]
```

Key phai gom it nhat:

```text
provider + model + prompt_version + input_mode + table_view_hash
```

Neu system prompt/schema thay doi thi cache cu khong duoc tai su dung.

### Cau hinh de nghi

```yaml
context_cache:
  mode: explicit          # off | implicit | explicit
  ttl_seconds: 900
  min_queries_per_table: 2
  on_unsupported: implicit # error | implicit | off
```

Can mo rong validation trong `config.py`. Competition profile nen khoa ro mode de
run co the tai lap.

### Xu ly loi va lifecycle

- Chi tao cache khi workbook co tu `min_queries_per_table` query tro len.
- Neu context nho hon nguong cache cua model, fallback ve implicit/off.
- Neu tao cache loi do unsupported, fallback theo config; khong fallback am tham.
- Neu cache het TTL giua run, tao lai mot lan va retry query hien tai.
- Khong log `remote_name` neu no duoc coi la identifier nhay cam; tuyet doi khong
  log API key.
- `finally` cleanup cache khi provider/API cho phep. TTL van la lop cleanup cuoi.
- HTTP retry phai tai su dung cung prepared context, khong tao cache moi.
- Plan repair chi gui failed plan/error dong va tai su dung context cache.

### Uu/nhuoc diem

- Cache hit co chu dich va do luong duoc.
- Rat hop voi workload nhieu query tren mot workbook lon.
- Co storage/write cost va TTL; workbook chi co mot query co the dat hon.
- Phu thuoc model/provider. Cached tokens van chiem context window.
- Can integration test bang fake SDK cho create/use/expire/delete/fallback.

## Phuong an C - Batch nhieu query cua mot workbook trong mot LLM request

### Y tuong

Gui table mot lan va yeu cau LLM tra ve mot mang plan, moi phan tu ung voi mot
query. Neu mot workbook co `Q` query, table input giam tu xap xi `Q * T` xuong `T`.

### Trien khai

1. Group `work` theo resolved workbook path.
2. Tao schema moi `plan_batch_v1`:

   ```json
   {
     "version": "plan_batch_v1",
     "table_id": "...",
     "plans": [
       {"query_id": "...", "steps": [], "return": "$s1"}
     ]
   }
   ```

3. Validate:

   - Moi query ID xuat hien dung mot lan.
   - Khong co query ID la.
   - Moi plan van qua `validate_plan()` rieng.

4. Execute tung plan doc lap bang executor hien tai.
5. Neu mot plan loi, repair rieng plan do. Repair dung explicit/implicit cache de
   khong gui lai table voi gia day du.
6. Chia batch theo gioi han:

   - `max_queries_per_batch`.
   - Uoc luong `max_output_tokens` cho tong so plan.
   - Tach batch neu table + questions gan context limit.

### Uu/nhuoc diem

- Giam input token thuc su rat manh.
- Mot response dai de bi truncation; mot schema error co the anh huong ca batch.
- Model co the bo sot/nham query ID va chat luong tung plan co the giam.
- Can thay doi schema, prompt, validation, telemetry va co che repair.

Nen benchmark batch size 2, 4 va 8 thay vi batch toan bo ngay tu dau.

## Phuong an D - Query-aware table slicing (retrieval local)

### Y tuong

Thay vi full-table, local code chon sheet/range/cot/hang lien quan den cau hoi va
chi render subset. Day la toi uu token thuc su va khong phu thuoc provider cache.

### Cac muc trien khai

#### D1. Heuristic local

- Tokenize/casefold cau hoi.
- Match sheet name, title, headers va row labels.
- Lay cac range match kem header ancestors, merged ranges, unit rows va mot cua so
  hang xung quanh.
- Neu do tin cay thap hoac co nhieu match, fallback full table.

#### D2. Hai pha locator -> planner

1. Request locator chi nhan workbook catalog nho: sheet names, used ranges,
   headers/section labels va row-label index.
2. Local code lay cac range duoc chon.
3. Planner nhan subset co toa do goc va lap plan nhu hien tai.

Can schema `table_slice_request_v1`, gioi han so range va validate moi selector de
LLM khong the doc ngoai workbook/range cho phep.

### Yeu cau correctness

Subset phai giu:

- Spreadsheet coordinates goc.
- Multi-row/merged header context.
- Units, subtotal/parent-child labels va footnote lien quan.
- Toan bo candidate rows khi query can ranking/aggregate tren tat ca doi tuong.

Day la phuong an rui ro nhat ve exact-match: retrieval thieu mot row co the tao
answer hop le ve format nhung sai du lieu. Bat buoc benchmark va fallback full
table khi ambiguous.

## Phuong an E - LLM tra cuu table qua tools

### Y tuong

Khong gui full table. Prompt chi co workbook catalog nho; LLM goi cac tool local:

- `list_sheets`
- `search_headers`
- `search_labels`
- `get_range`
- `get_cell`

Sau khi dinh vi du lieu, LLM tao plan hoac thuc thi tren tool registry hien tai.

### Trien khai

- Them agent/tool-call loop co gioi han `max_lookup_steps` va `max_cells_returned`.
- Tool chi doc `TableContext` trong memory, khong cap filesystem access cho model.
- Moi tool result phai giu coordinates, merged-header context va units.
- Log tool args/result hash de audit, khong log table day du neu khong can.
- Neu het budget hoac lookup ambiguous, fallback full-table planner.

Phuong an nay giam token cho workbook rat lon nhung tang so API round-trip, do tre
va do phuc tap. No phu hop giai doan sau hon la toi uu dau tien.

## Phuong an khong nen coi la giai phap chinh

### Chi giu Python `table_cache`

Khong anh huong prompt token; no chi tranh doc/parse file lai.

### Gui `table_id` roi mong model nho table tu request truoc

Stateless API khong dam bao model con context. Neu dung conversation history thi
table van nam trong history/context va co the van bi tinh token, tru khi provider
cache no.

### Nen/gzip/base64 table trong prompt

Model khong tu giai nen dang binary; base64 thuong tang token va lam giam accuracy.

### Tom tat table mot lan bang LLM

Tom tat co the lam mat cell/row can cho query sau, khong phu hop khi can exact QA
neu khong co co che tra cuu/fallback vao du lieu goc.

## 4. Lo trinh de xuat

### Giai doan 0 - Do baseline

Them telemetry theo query va workbook:

- `table_view_chars` va token estimate/count neu SDK ho tro.
- `queries_per_workbook`.
- `prompt_tokens`, `cached_prompt_tokens`, `uncached_prompt_tokens`.
- `actual_cost_usd`, latency, exact match va error rate.
- Cache hit/miss va ly do fallback.

Bao cao theo workbook de thay ro workbook nao dang gay ton token.

### Giai doan 1 - Reorder prefix + group query theo workbook

Day la thay doi nen lam truoc vi nho va it anh huong logic:

1. Dua static table context len truoc dynamic query.
2. Them `INPUT_MODE` vao direct prompt de hai flow nhat quan.
3. Xu ly query cung workbook lien tiep, khoi phuc output order sau cung.
4. Them sticky cache key cho OpenRouter neu duoc bat trong config.
5. Benchmark A/B voi prompt cu.

Dieu kien dat:

- Exact match khong giam qua nguong chap nhan.
- `cached_prompt_tokens > 0` tren cac request thu hai tro di cua cung workbook khi
  model du dieu kien.
- Ket qua va submission van dung thu tu/contract.

### Giai doan 2 - Explicit Gemini cache theo workbook

Repo dang co Gemini client va da normalize `cached_content_token_count`, nen day la
provider dau tien hop ly de lam explicit caching:

1. Them `ContextCacheSpec`/`PreparedContext` va capability interface.
2. Implement `GeminiClient.prepare_context()` bang `client.caches.create()`.
3. Generate bang `config.cached_content` va chi gui dynamic query.
4. Cache theo `table_view_hash`; reuse cho initial call, retry va repair.
5. Them fallback, TTL renewal va cleanup.
6. Fake-SDK integration tests cho lifecycle va usage telemetry.

Luu y: phai kiem tra model dang dung co ho tro explicit cache va context co vuot
nguong toi thieu hay khong. Neu khong, dung implicit prefix caching.

### Giai doan 3 - Batch query theo workbook

Trien khai sau khi caching/telemetry on dinh. Bat dau batch nho va chi mo rong neu:

- Ty le response truncation thap.
- So plan thieu/duplicate bang 0.
- Exact match khong giam.
- Tong token va cost giam ro rang so voi explicit cache don-query.

### Giai doan 4 - Retrieval/table tools

Chi can khi table qua lon, cache khong du hoac muc tieu la giam context thuc su.
Trien khai D1 heuristic co fallback truoc; sau do moi can nhac locator/tool loop.

## 5. Khuyen nghi cuoi cung cho repo nay

Thu tu uu tien:

1. **Reorder static prefix + group theo workbook**: quick win, it rui ro.
2. **Explicit Gemini cache theo workbook**: giam chi phi co chu dich, phu hop code
   hien tai va telemetry da co `cached_prompt_tokens`.
3. **Batch 2-4 query/workbook**: giam token thuc su, nhung can benchmark accuracy.
4. **Query-aware slicing/tools**: toi uu lon nhat cho table lon, doi lai correctness
   va implementation risk cao nhat.

Neu muc tieu gan nhat la **giam hoa don ma giu accuracy**, chon (1) + (2). Neu muc
tieu la **giam tong context/input tokens bat ke provider**, can them (3), va ve lau
dai la (4).

## 6. Ke hoach test bat buoc

### Unit tests

- Static prefix giong nhau cho query cung workbook.
- Dynamic suffix chua dung query ID/question/answer spec.
- Cache key thay doi khi model, prompt version, mode hoac table hash thay doi.
- Khong reuse cache giua hai workbook.
- Expired/unsupported cache fallback dung policy.
- Repair va retry tai su dung prepared context.

### Integration tests

- Fake Gemini SDK: create mot lan, generate nhieu lan, delete mot lan.
- Provider bao cached token duoc normalize vao `TokenUsage`.
- Query order bi group noi bo nhung `results.jsonl`/submission dung thu tu yeu cau.
- Mot query loi khong lam mat ket qua cua query khac cung workbook.

### Benchmark acceptance

So sanh tren cung query set va model:

```text
exact_match
format_valid_rate
failed queries
prompt_tokens_total
cached_prompt_tokens_total
uncached_prompt_tokens_total
actual_cost_usd
p50/p95 latency
```

Khong merge toi uu token neu chi phi giam nhung exact match hoac format validity
giam ngoai nguong da chot.

## 7. Tai lieu provider tham khao

- Gemini context caching:
  https://ai.google.dev/gemini-api/docs/generate-content/caching
- OpenRouter prompt caching:
  https://openrouter.ai/docs/guides/best-practices/prompt-caching

Tinh nang, model support, minimum cacheable tokens, TTL va pricing co the thay doi.
Can xac minh lai tai lieu provider truoc khi khoa model/config production.
