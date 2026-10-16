# Chay Gemini API an toan theo RPM va TPM

## 1. Muc tieu

Khong can ep moi query cach nhau 60 giay. Muc tieu dung la:

- Tong request trong cua so 60 giay khong vuot RPM.
- Tong input token trong cua so 60 giay khong vuot TPM.
- Initial call, retry, repair va output retry deu bi kiem soat.
- Chi cho khi request tiep theo se lam vuot RPM hoac TPM.
- Thoi gian cho quota khong tinh vao active query/run timeout.
- Neu Gemini van tra 429, ton trong `Retry-After` va khong retry vo han.
- RPD chi can bao loi va dung; khong checkpoint/resume.

Tai lieu Google:

- Rate limits: https://ai.google.dev/gemini-api/docs/rate-limits
- API errors: https://ai.google.dev/gemini-api/docs/api-errors

Quota Gemini duoc ap dung theo project, khong phai rieng tung API key. RPM/TPM
thuc te phu thuoc model va usage tier, nen nguoi dung phai lay gia tri hien tai
tu Google AI Studio.

## 2. Mot quota gate duy nhat

Khong can hai lop query gate va request gate. Chi can mot `GeminiQuotaGate` duoc
goi truoc **moi API request**:

```text
query 1 initial ------> quota gate ------> Gemini
query 1 repair -------> quota gate ------> Gemini
query 2 initial ------> quota gate ------> Gemini
HTTP retry -----------> quota gate ------> Gemini
```

Quota gate kiem tra cung luc:

```text
requests_in_last_60_seconds + 1 <= RPM
input_tokens_in_last_60_seconds + estimated_request_tokens <= TPM
```

Neu ca hai dieu kien dung, request chay ngay. Neu mot dieu kien sai, gate chi cho
den luc cac record cu can thiet roi khoi cua so 60 giay.

Vi runner hien chay tuan tu (`max_workers=1`), khong can limiter rieng theo query.

## 3. Vi du

Gia su:

```text
RPM = 5
TPM = 100,000
```

Ba request dau co input token:

```text
t=0s:  20,000 tokens
t=5s:  25,000 tokens
t=9s:  30,000 tokens
```

Tai t=12s, request moi uoc luong 35,000 tokens:

```text
request count = 4 <= 5 RPM
token total   = 20k + 25k + 30k + 35k = 110k > 100k TPM
```

Gate phai cho den request 20k tai t=0 roi khoi cua so. Sau do:

```text
25k + 30k + 35k = 90k <= 100k
```

Request duoc gui. Khong can cho du 60 giay tinh tu request gan nhat.

Neu token van du TPM nhung da co 5 request trong cua so, gate cho den request cu
nhat roi khoi cua so de dam bao RPM.

## 4. Cau hinh

Them vao Gemini provider options:

```yaml
llm:
  provider: gemini
  model: gemini-3.1-flash-lite
  provider_options:
    gemini:
      api_key_env: GEMINI_API_KEY
      api_surface: generate_content
      thinking_level: low
      rate_limit:
        enabled: true
        requests_per_minute: 5
        input_tokens_per_minute: 100000
        window_seconds: 60
        token_estimate_safety_factor: 1.25
        max_wait_seconds: 120
```

Y nghia:

- `requests_per_minute`: RPM hien tai cua project/model trong AI Studio.
- `input_tokens_per_minute`: TPM hien tai cua project/model.
- `window_seconds`: mac dinh 60 giay.
- `token_estimate_safety_factor`: reserve cao hon estimate de tranh vuot TPM do
  uoc luong thap.
- `max_wait_seconds`: khong cho vo han trong mot admission. Neu can cho lau hon,
  dung va bao loi ro rang.

Khong hard-code gia tri 5/100000 trong source. Day chi la vi du; config phai dung
quota thuc cua API key project.

## 5. Data structure

Them file:

```text
src/table_qa/rate_limit.py
```

Moi request duoc ghi trong deque:

```python
@dataclass
class QuotaRecord:
    started_at: float
    reserved_input_tokens: int


@dataclass(frozen=True)
class Admission:
    waited_seconds: float
    wait_reason: str | None  # rpm | tpm | rpm+tpm
    reserved_input_tokens: int
    record_id: str
```

Gate interface:

```python
class RequestQuotaGate(Protocol):
    def acquire(self, estimated_input_tokens: int) -> Admission: ...
    def commit(self, admission: Admission,
               actual_input_tokens: int | None) -> None: ...
```

Co hai implementation:

- `GeminiSlidingWindowGate`: kiem soat RPM + TPM.
- `NoopQuotaGate`: dung cho mock/OpenRouter hoac khi limiter disabled.

Dung `time.monotonic()` hoac `time.perf_counter()`, khong dung wall clock de tinh
cua so.

Neu sau nay co concurrency, bao ve deque bang `threading.Lock`. Hien tai runner
tuan tu nhung lock giup implementation khong co race khi `max_workers` thay doi.

## 6. Thuat toan acquire

Pseudo-code:

```python
def acquire(estimated_input_tokens):
    reserved = ceil(estimated_input_tokens * safety_factor)

    if reserved > tpm_limit:
        raise CONTEXT_TOO_LARGE

    while True:
        now = clock()
        evict records with started_at <= now - window_seconds

        rpm_ok = len(records) + 1 <= rpm_limit
        tpm_ok = sum(r.reserved_input_tokens for r in records) + reserved <= tpm_limit

        if rpm_ok and tpm_ok:
            append reservation before releasing lock
            return admission

        wait_rpm = time until enough request records expire
        wait_tpm = time until enough token reservations expire
        wait_for = max(required waits for violated constraints)

        if wait_for > max_wait_seconds:
            raise API_RATE_LIMIT

        sleep(wait_for + small_clock_margin)
```

Can tinh thoi gian TPM bang cach duyet record tu cu den moi va loai dan cho den khi
du token capacity. Khong chi lay record cu nhat neu can loai nhieu record.

Reservation phai duoc append truoc khi tha lock de hai request khong cung nhin
thay mot capacity trong.

## 7. Token estimate va reconciliation

TPM tinh theo input token, nen can estimate truoc khi request duoc gui.

### Phuong an don gian

Uoc luong local tu prompt:

```python
raw_estimate = ceil(len((system_prompt + user_prompt).encode("utf-8")) / 4)
reserved = ceil(raw_estimate * safety_factor)
```

Day chi la estimate. Unicode, con so va serialized table co the khac, vi vay can
safety factor.

### Sau response

Repo da normalize Gemini usage:

```python
response.usage.prompt_tokens
```

Sau request thanh cong:

```python
gate.commit(admission, response.usage.prompt_tokens)
```

Gate cap nhat reservation:

- Neu actual > reserved: tang record len actual va ghi warning neu tong cua so da
  vuot TPM do estimate thap.
- Neu actual < reserved: giam record de request tiep theo khong cho thua.
- Neu provider khong tra usage: giu reservation.

Co the dung Gemini count-tokens endpoint de chinh xac hon, nhung no tao them
network call va co the co quota/latency rieng. Local estimate + actual reconciliation
la phuong an don gian cho pipeline nay.

## 8. Tich hop vao runner

Goi gate ngay truoc tung API attempt trong `generate()`:

```python
estimated = estimate_input_tokens(attempt_request)
admission = quota_gate.acquire(estimated)

try:
    response = backend.generate(attempt_request)
except Exception:
    # Request da duoc gui hoac co the da den provider, giu reservation de an toan.
    raise
else:
    quota_gate.commit(admission, response.usage.prompt_tokens)
```

Khong goi gate mot lan ben ngoai query loop, vi nhu vay repair/retry se thoat khoi
limiter.

Khong `sleep(60)` sau query. Gate tu tinh chinh xac can cho bao lau dua tren cua so
RPM/TPM hien tai.

## 9. Thoi gian cho khong tinh vao active timeout

Runner hien dung:

```python
time.perf_counter() - started_query
```

Neu gate sleep, wait se bi tinh vao query/run timeout. Can tach:

```text
active elapsed = wall elapsed - quota wait
```

Them `ActiveTimer`:

```python
class ActiveTimer:
    def __init__(self, clock):
        self.clock = clock
        self.started_at = clock()
        self.excluded_seconds = 0.0

    def exclude(self, waited_seconds):
        self.excluded_seconds += waited_seconds

    def elapsed(self):
        return self.clock() - self.started_at - self.excluded_seconds
```

Theo doi rieng:

- `query_active_elapsed_ms`.
- `query_quota_wait_ms`.
- `query_wall_elapsed_ms`.
- `run_active_elapsed_ms`.
- `run_quota_wait_ms`.
- `run_wall_time_ms`.

`query_timeout_seconds` va `run_timeout_seconds` dung active elapsed. Network,
model, parse va executor time van tinh binh thuong.

## 10. Xu ly 429

Local limiter giam nguy co nhung khong dam bao provider khong tra 429, vi:

- Quota co the dang duoc dung boi process/nguoi dung khac trong cung project.
- Estimate token co the thap.
- Google khong dam bao capacity thuc te luon bang quota danh nghia.

Khi gap 429:

1. Doc structured reason va `Retry-After` neu co.
2. Neu la minute rate limit, cho theo `Retry-After`/backoff.
3. Lan retry van phai qua `GeminiSlidingWindowGate`.
4. Khong vuot `max_api_attempts`.
5. Quota wait do 429 duoc ghi rieng va khong tinh active timeout.

Neu structured error cho biet daily quota/RPD exhausted:

- Dung run ngay.
- Khong retry sau vai giay vi quota ngay chua reset.
- Bao loi ro rang de nguoi dung chay lai sau.
- Khong checkpoint/resume.

## 11. RPD

Khong can local RPD manager. Chi can:

- Ghi `total_llm_calls` (runner da co).
- Co the canh bao truoc run:

  ```text
  minimum requests = number of queries
  maximum configured attempts = queries * retry/repair allowance
  ```

- Neu provider bao daily quota exhausted, dung.

RPD khong anh huong thuat toan RPM/TPM sliding window.

## 12. Telemetry

Log khi gate phai cho:

```json
{
  "event": "quota_wait_started",
  "reason": "tpm",
  "wait_seconds": 23.4,
  "requests_in_window": 3,
  "tokens_in_window": 91000,
  "estimated_request_tokens": 24000
}
```

Sau wait:

```json
{
  "event": "quota_wait_finished",
  "reason": "tpm",
  "waited_seconds": 23.5
}
```

Moi query result them:

```json
{
  "quota_wait_ms": 0,
  "active_elapsed_ms": 0,
  "wall_elapsed_ms": 0,
  "estimated_input_tokens": 0,
  "actual_input_tokens": 0
}
```

Summary them:

```json
{
  "quota_wait_ms_total": 0,
  "run_active_elapsed_ms": 0,
  "run_wall_time_ms": 0,
  "rate_limit": {
    "requests_per_minute": 5,
    "input_tokens_per_minute": 100000,
    "window_seconds": 60
  }
}
```

Khong log API key hoac Authorization header.

## 13. Cac file can sua

### `src/table_qa/rate_limit.py`

- `GeminiSlidingWindowGate`.
- `NoopQuotaGate`.
- Token estimator.
- Active timer/helper.
- Injectable clock va sleeper.

### `src/table_qa/config.py`

- Them nested `rate_limit` cho Gemini.
- Validate RPM/TPM/window/safety factor/max wait la so duong.

### `src/table_qa/llm/factory.py`

- Tao quota gate tu provider config.

### `src/table_qa/runner.py`

- Acquire truoc moi API attempt.
- Commit actual usage sau response.
- Loai quota wait khoi active deadline.
- Ghi telemetry.

### `src/table_qa/llm/errors.py`

- Bao toan `Retry-After` va structured quota reason.
- Phan biet minute rate limit voi daily quota neu SDK cung cap.

### `config/benchmark_gemini.yaml`

- Dien RPM va TPM thuc cua project.

## 14. Test khong cho that

Inject fake clock:

```python
class FakeClock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds
```

Test cases:

1. Request chay ngay khi RPM va TPM con du.
2. Cham RPM thi cho den record request can thiet het han.
3. Cham TPM thi cho den du token records het han.
4. Neu ca RPM va TPM deu cham, cho den khi ca hai cung hop le.
5. Khong ep query/request cach nhau 60 giay khi quota con du.
6. Repair va retry cung bi gate.
7. Request rieng le lon hon TPM fail ngay, khong sleep vo han.
8. Actual token thap hon estimate giai phong capacity.
9. Actual token cao hon estimate cap nhat ledger va ghi warning.
10. Quota wait khong tang active timeout.
11. Wall time van bao gom quota wait.
12. 429 retry van qua gate va khong vuot max attempts.
13. Daily quota exhausted dung, khong checkpoint/resume.
14. OpenRouter/mock dung no-op gate.

## 15. Lo trinh trien khai

### Phase 1 - RPM + TPM gate

1. Them config RPM/TPM.
2. Them sliding-window deque.
3. Them token estimate + reservation.
4. Acquire truoc moi API attempt.
5. Commit provider usage.

### Phase 2 - Active time va telemetry

1. Them active timer.
2. Loai gate wait khoi query/run timeout.
3. Ghi active/wall/wait metrics.

### Phase 3 - 429 hardening

1. Bao toan `Retry-After` va quota reason.
2. Retry minute quota co gioi han.
3. Dung ngay voi daily quota.

## 16. Ket luan

Giai phap can trien khai la **mot request-level sliding-window gate** cho Gemini,
kiem soat cung luc RPM va TPM.

Khong can:

- Query gate.
- Moi query cach nhau 60 giay.
- Moi request cach nhau co dinh 60 giay.
- Checkpoint/resume.
- Persistent quota state.

Request chi cho khi no thuc su lam vuot RPM hoac TPM. Day la cach vua an toan vua
khong lam pipeline cham hon can thiet.
