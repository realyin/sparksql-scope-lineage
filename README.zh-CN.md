# sparksql-scope-lineage

中文 | [English](README.md)

面向 Spark SQL 和 Hive 风格数仓 SQL 的字段级血缘解析工具。

`sparksql-scope-lineage` 会静态解析 SQL，保留 CTE、子查询、UNION 分支等中间查询块，支持在提供表结构信息时展开 `SELECT *`，并提供审计工具检查输出结果的结构完整性。

它适合回答这些问题：

- 目标表的某个字段来自哪些物理表字段？
- 这个字段经过了哪些 CTE、子查询或 UNION 分支？
- `SELECT *` 是否已经完整展开，还是缺少表结构信息？
- 血缘图里有没有 UNKNOWN 来源、断链或不完整引用？

## 为什么需要它

真实数仓里的 Spark SQL 很少只是简单的：

```sql
SELECT col FROM table
```

更常见的是：

- 很长的 `WITH` 链路；
- 多层嵌套子查询；
- 按位置对齐的 `UNION ALL`；
- 窗口函数、聚合函数和 CASE 表达式；
- `LATERAL VIEW`、`explode` 这类生成函数；
- `MERGE INTO`；
- 从物理表或中间结果里 `SELECT *`。

如果一开始就把所有字段直接拍平成“目标字段 -> 物理字段”，复杂 SQL 会很难排查。这个项目的做法是先保留每个查询块，也就是 scope，再从 scope 图里追踪到最终的物理字段。

这样做有两个好处：

- 中间转换不会丢，调试时能看到字段是在哪一层变了；
- 出现 UNKNOWN 或断链时，可以定位到具体的 CTE、子查询或 UNION 分支。

## 快速开始

本地开发安装：

```bash
python -m pip install -e ".[dev]"
```

解析一个 SQL 文件：

```bash
scope-lineage parse \
  --sql-file examples/simple_insert.sql \
  --out /tmp/scope-lineage-demo \
  --md \
  --html
```

如果 SQL 里有 `SELECT *`，可以提供表结构信息：

```bash
scope-lineage parse \
  --sql-file examples/select_star_with_schema.sql \
  --schema examples/table_cols.csv \
  --out /tmp/scope-lineage-star-demo \
  --md \
  --html
```

运行测试：

```bash
python -m pytest -q
```

## Python API

```python
from lineage_parser import parse_all_scope_lineage

sql = """
INSERT OVERWRITE TABLE mart.user_summary
WITH active_users AS (
  SELECT id, country FROM ods.users WHERE status = 'active'
)
SELECT id, country FROM active_users
"""

results = parse_all_scope_lineage(sql, task_name="user_summary")
root = results[0].scopes["ROOT"]

for column in root.columns:
    print(column.name, column.sources)
```

## 命令行工具

### 解析单个 SQL 文件

```bash
scope-lineage parse \
  --sql-file examples/simple_insert.sql \
  --out /tmp/scope-lineage-demo \
  --md \
  --html
```

### 批量解析任务目录

任务文件是 JSON，至少包含 `task_name` 和 `sql`：

```json
{
  "task_name": "simple_insert",
  "sql": "INSERT OVERWRITE TABLE mart.t SELECT id FROM ods.users"
}
```

运行：

```bash
python tools/run_scope_corpus.py \
  --input-dir examples/tasks \
  --out /tmp/scope-output \
  --schema examples/table_cols.csv \
  --md \
  --html
```

### 审计输出结果

```bash
python tools/audit_scope_output.py \
  --out-dir /tmp/scope-output/tasks \
  --report /tmp/scope-audit.md \
  --json /tmp/scope-audit.json \
  --fail-on-red
```

审计结果分为三类：

| 等级 | 含义 |
| --- | --- |
| RED | 存在结构性问题，比如 UNKNOWN 来源或内部引用断链 |
| YELLOW | 结果可用，但有明确的准确性或完整性边界 |
| GREEN | 未发现结构问题或明显告警 |

### 比较两份输出

在修改 parser 前后，可以用它确认输出是否发生变化：

```bash
python tools/compare_scope_outputs.py \
  --left /tmp/scope-output-before/tasks \
  --right /tmp/scope-output-after/tasks \
  --report /tmp/scope-compare.md \
  --json /tmp/scope-compare.json \
  --fail-on-diff
```

### 汇总多份审计报告

```bash
python tools/summarize_audit_reports.py \
  --audit /tmp/dwd-audit.json \
  --audit /tmp/support-audit.json \
  --report /tmp/scope-audit-summary.md \
  --json /tmp/scope-audit-summary.json
```

## 输出文件

每条 SQL 语句会生成一组结果：

```text
lineage.json
profile.json
diagnostics.json
report.html
lineage.md
views/
  scope_overview.mmd
  field_lineage.mmd
  physical.mmd
  per_column/*.mmd
```

`lineage.json` 是完整的机器可读血缘结果，包含所有中间 `scopes`、`scope_graph`、
diagnostics、`scope_profile`，以及 ROOT 字段到物理表字段的端到端血缘。

`profile.json` 是给 LLM/任务画像使用的轻量产物。它不包含完整的中间 `scopes`
明细，只保留解释 SQL 加工逻辑所需的信息：

- `scope_graph`：scope 级 DAG；
- `scope_profile`：每个 scope 一步加工摘要，包含 role、operations、物理源表、
  joins、filters、aggregations、window、CASE 摘要、关键重命名、DISTINCT 标记、
  UNION 分支数和 lateral view 展开信息；解析器产生的纯透传 scope 会被过滤，
  `profile_step_count` 只统计保留下来的画像步骤；
- `related_metadata`：拆分为 `input_tables` 和 `output_tables`。输入表优先使用
  schema 中的 `type/comment`，schema 缺失时从 scope 引用字段补齐；遇到星号或
  未解析等不确定引用时，会保守保留该表全部已知字段；
- `root_columns`：最终输出字段；
- `end_to_end_lineage`：ROOT 字段追溯到物理表字段，并带 `trace_complete`；
  遇到未展开星号等中断场景时会给出原因；
- `diagnostics`：warning 和解析置信度信号。

`report.html` 是自包含的离线可视化报告，包含 scope DAG、ROOT 字段表、单字段
聚焦血缘和 diagnostics。它不依赖 CDN、字体、脚本或本地旁路文件，可以直接在
受限内网环境打开。

Mermaid 文件主要用于人工检查和调试。

## 基本原理

核心思路很简单：**把一条 SQL 看成由多个 scope 组成的图**。

常见 scope 例子：

| SQL 结构 | scope id 示例 |
| --- | --- |
| 写入目标表的最外层 SELECT | `ROOT` |
| `WITH users AS (...)` | `cte:users` |
| `FROM (SELECT ...) s` | `subq:s` |
| `SELECT ... UNION ALL SELECT ...` | `union:main` |
| 第一个 UNION 分支 | `union:main:b01` |
| 物理表 | `ods.users` |

每个 scope 都有自己的输出字段。字段来源只指向直接上游 scope 或物理表；需要完整物理血缘时，再沿着 scope 图继续向上追。

处理流程大致是：

```text
SQL
  -> sqlglot 解析
  -> 构建 scope tree
  -> 分配稳定的 scope id
  -> 解析每个 scope 的输出字段
  -> 按位置对齐 UNION 分支
  -> 在有 schema 时展开 SELECT *
  -> 生成 scope 图和 diagnostics
  -> 派生轻量 scope profile 和端到端物理血缘
  -> 输出 JSON / HTML / Mermaid / Markdown
  -> 审计输出一致性
```

更多说明：

- [Scope 模型](docs/scope-model.md)
- [原理说明](docs/how-it-works.zh-CN.md)
- [Schema 元数据](docs/schema-metadata.md)
- [审计方法](docs/audit-methodology.md)
- [限制说明](docs/limitations.md)

## Schema 元数据

没有表字段信息时，`SELECT *` 无法完整展开。可以提供 CSV：

```csv
table_name,column_name
ods.users,id
ods.users,country
ods.users,status
```

如果 CSV 中包含可选的 `type` 和 `comment`，会保留到 `related_metadata`：

```csv
table_name,column_name,type,comment
ods.users,id,bigint,用户ID
ods.users,status,string,账号状态
```

也可以提供 JSON：

```json
{
  "ods.users": ["id", "country", "status"]
}
```

也支持更完整的 JSON 元数据：

```json
{
  "ods.users": {
    "column_details": [
      {"name": "id", "type": "bigint", "comment": "用户ID"}
    ]
  }
}
```

加载时会对三段式表名做归一化，去掉 catalog 部分。因此 `catalog.db.table` 和 `db.table` 可以匹配到同一张表。

## 当前支持

已有测试和示例覆盖：

- `INSERT` / `INSERT OVERWRITE`
- `MERGE INTO` 的 update 和 insert 分支
- CTE 链路
- 嵌套子查询
- `UNION ALL`
- 带表别名和不带表别名的字段引用
- `SELECT *` / `alias.*`
- CASE 表达式
- 聚合函数
- 窗口函数
- 部分 Spark/Hive 生成函数
- Mermaid 和 JSON 输出校验

## 限制

这是静态解析工具，不是 Spark 运行时。

- 不模拟运行时行为。
- 完整展开 `SELECT *` 需要外部 schema。
- 缺少 schema 时，不带表别名的字段可能存在歧义。
- 一些方言扩展需要逐步补支持。
- MERGE DELETE 属于行级操作，会作为 diagnostic 报出，不生成目标字段血缘。

详见 [docs/limitations.md](docs/limitations.md)。

## 项目状态

项目还处在早期阶段，但已经可以使用。当前重点是：

- 扩大 Spark SQL 覆盖范围；
- 保持 diagnostics 清晰可解释；
- 增加合成回归用例；
- 让 audit 输出更适合真实项目验收。

欢迎贡献不包含私有业务数据的最小复现 SQL。

## License

Apache-2.0
