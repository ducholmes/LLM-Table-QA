# LLM Table QA

Chuong trinh doc `queries.json` va cac workbook `.xlsx`, sau do tao file
`submission.csv` gom hai cot `id,answer`.

## 1. Chuan bi input

Thu muc input can co cau truc:

```text
private_data/
├── queries.json
└── xlsx_files/
    ├── table_01.xlsx
    ├── table_02.xlsx
    └── ...
```

Moi phan tu trong `queries.json` can co ba field:

```json
[
  {
    "id": 1,
    "file_name": "table_01.xlsx",
    "query": "Question about the workbook"
  }
]
```

`file_name` phai trung voi ten file trong `xlsx_files/`. Chuong trinh chap nhan
`id` dang so hoac chuoi va khong gioi han co dinh so query/workbook.

Neu cac file `.xlsx` nam truc tiep canh `queries.json` thay vi trong
`xlsx_files/`, co the bo tham so `--workbook-dir` khi chay.

## 2. Cai dat

Yeu cau Python 3.11 tro len. Tu thu muc goc cua repository, chay:

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -e '.[openrouter]'
```

Lenh `pip install -e` cai package `table_qa` vao virtual environment, vi vay cac
lenh chay ben duoi khong can khai bao `PYTHONPATH`.

## 3. Cau hinh OpenRouter

Tao file `.env` tai thu muc goc repository:

```env
OPENROUTER_API_KEY=your_openrouter_api_key
```

Khong commit hoac gui file `.env` kem bai nop.

## 4. Chay voi private data

Vi du input nam tai `data/Public data 2` va workbook nam trong thu muc con
`xlsx_files`:

```bash
python3 -m table_qa.main \
  --config config/submission.yaml \
  --input-dir data/Public data 2 \
  --workbook-dir xlsx_files \
  --output output/submission.csv
```

`--input-dir` ho tro path co dau cach ma khong bat buoc dung dau nhay. Van co the
dung dang co dau nhay neu muon:

```bash
python3 -m table_qa.main \
  --config config/submission.yaml \
  --input-dir "/absolute/path/to/private data" \
  --workbook-dir xlsx_files \
  --output "/absolute/path/to/output/submission.csv"
```

Neu workbook nam truc tiep trong thu muc input:

```bash
python3 -m table_qa.main \
  --config config/submission.yaml \
  --input-dir /absolute/path/to/private_data \
  --output /absolute/path/to/output/submission.csv
```

## 5. Output

File ket qua duoc ghi dung tai path truyen qua `--output`:

```text
output/submission.csv
```

Dinh dang:

```csv
id,answer
1,final answer
2,another final answer
```

Thu tu ID trong output giong thu tu trong `queries.json`. Truong `answer` chi
chua dap an cuoi cung, khong kem giai thich.

Log, usage va thong tin chan doan duoc ghi trong thu muc `artifacts/` nam canh
file output. Vi du:

```text
output/
├── submission.csv
└── artifacts/
    └── <run-id>/
```

Exit code `0` nghia la tat ca query thanh cong. Exit code `1` nghia la mot hoac
nhieu query loi; neu dung submission profile, CSV van co mot dong cho moi ID va
answer cua query loi se de trong.
