# PALS DAG Unified v1 交付包

本目录包含三个独立交付包。每个包内均包含：

- `pals_dag_unified_v1.jsonl`：一题一行的统一数据
- `pals_dag_unified_v1.html`：可视化查看器
- `manifest.json`：条数、子集分布和 SHA256
- `accepted_source.jsonl`：导出时冻结的已接受源记录

| 交付包 | 条数 | 子集 | JSONL SHA256 |
|---|---:|---|---|
| `gsm8k-final-20260923.tar.gz` | 530 | GSM8K main/test | `5c80ca876e0b2bfa09f945b729013b5c710dbe6e8ccffb760df9f3ffb1c42c09` |
| `mmlu-math-5-subsets-final-20260923.tar.gz` | 415 | abstract_algebra, college_mathematics, elementary_mathematics, high_school_mathematics, high_school_statistics | `11e8cc9c455ea5aaf18724514e4d21f76d1416e69f8c36c4376ebddeec79d844` |
| `mmlu-psych-social-4-subsets-final-20260923.tar.gz` | 594 | high_school_psychology, professional_psychology, sociology, human_sexuality | `e276c88ae107a49e5a8260b9c6a0f9c10f5d20bdada6843d9eb7183107b621dc` |

总计 1,539 条。当前交付范围为 `model_accepted` 的 synthetic reference DAG，`human_approved_records=0`，不能标记为 human-approved gold。
