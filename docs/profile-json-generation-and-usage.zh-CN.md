# profile.json 生成和使用说明

这份文档说明三件事：

1. 生成 `profile.json` 需要哪些输入信息，输出结构是什么；
2. 生成 `profile.json` 时应调用什么方法，以及 CLI/Python 如何调用；
3. 如何把 `profile.json` 交给 LLM，生成面向数据治理和交接的 Markdown 文档。

`profile.json` 是给 LLM 使用的轻量任务画像文件。它来自完整的 `lineage.json`，
但不会保留所有中间 scope 的逐字段明细，而是保留足够解释 SQL 业务逻辑的摘要、
scope 加工链路、规则候选、端到端血缘、元数据和风险边界。

## 1. 生成 profile.json 需要哪些信息

### 1.1 必需输入

生成 `profile.json` 至少需要：

- SQL 文本；
- 任务名 `task_name`。

单条 SQL 文件示例：

```sql
INSERT OVERWRITE TABLE mart.customer_profile
SELECT customer_id, dt
FROM ods.customer_detail
WHERE dt = '20260515';
```

任务 JSON 示例：

```json
{
  "task_name": "customer_profile_task",
  "sql": "INSERT OVERWRITE TABLE mart.customer_profile SELECT customer_id, dt FROM ods.customer_detail WHERE dt = '20260515'"
}
```

如果 SQL 中包含多条 `INSERT` 或多条可解析语句，解析器会按语句生成多个结果，
任务 ID 会带后缀，例如 `task_name#0`、`task_name#1`。

### 1.2 推荐输入：字段 schema

字段 schema 强烈建议提供。它用于：

- 展开 `SELECT *` 或 `alias.*`；
- 解析未限定字段来自哪张物理表；
- 在 `related_metadata` 中输出使用到的字段元数据；
- 给 LLM 提供字段类型和字段中文注释；
- 减少 `star_not_expanded`、`unresolved_unqualified_no_schema` 等边界。

CSV 支持的常用列：

```csv
table_name,column_name,type,comment
ods.customer_detail,customer_id,string,客户ID
ods.customer_detail,dt,string,日期分区
```

也兼容以下列名：

- 表名：`table_name` 或 `table`
- 字段名：`column_name` 或 `name`
- 类型：`type` 或 `column_type`
- 注释：`comment` 或 `column_comment`

JSON 支持几种形态：

```json
{
  "ods.customer_detail": [
    {"name": "customer_id", "type": "string", "comment": "客户ID"},
    {"name": "dt", "type": "string", "comment": "日期分区"}
  ]
}
```

或：

```json
{
  "tables": [
    {
      "table_name": "ods.customer_detail",
      "columns": [
        {"name": "customer_id", "type": "string", "comment": "客户ID"},
        {"name": "dt", "type": "string", "comment": "日期分区"}
      ]
    }
  ]
}
```

### 1.3 推荐输入：表级元数据

表级元数据用于让 LLM 理解表语义，而不只是看到英文表名。

CSV 支持的常用列：

```csv
table_name,table_name_cn,table_desc,table_label_layer
ods.customer_detail,客户明细表,客户基础属性和状态明细,ODS
mart.customer_profile,客户画像表,面向分析的客户画像结果表,APP
```

也兼容：

- 表名：`table_name` 或 `table`
- 中文名：`table_name_cn`
- 表说明：`table_desc` 或 `comment`
- 分层：`table_label_layer` 或 `layer`

JSON 示例：

```json
{
  "ods.customer_detail": {
    "table_name_cn": "客户明细表",
    "table_desc": "客户基础属性和状态明细",
    "table_label_layer": "ODS"
  }
}
```

### 1.4 profile.json 的主要结构

`profile.json` 顶层结构大致如下：

```json
{
  "task_name": "customer_profile_task#0",
  "target_table": "mart.customer_profile",
  "stmt_kind": "INSERT_OVERWRITE",
  "source_tables": ["ods.customer_detail"],
  "summary": {},
  "grain": {},
  "scope_profile": {"steps": []},
  "business_profile": {},
  "business_rule_candidates": [],
  "important_columns": [],
  "end_to_end_lineage": [],
  "related_metadata": {
    "input_tables": {},
    "output_tables": {}
  },
  "filters_summary": [],
  "expression_catalog": [],
  "diagnostics": {},
  "compact_policy": {},
  "read_order": []
}
```

核心字段说明：

| 字段 | 含义 | LLM 使用方式 |
| --- | --- | --- |
| `task_name` | 任务名，可能带 `#0` 后缀 | 文档标题和任务定位 |
| `target_table` | 写入目标表 | 识别输出对象 |
| `source_tables` | 物理输入表列表 | 识别上游来源 |
| `summary` | 任务规模和主要操作摘要 | 快速了解任务整体加工 |
| `grain` | 候选输出粒度字段 | 只能写“候选输出标识字段”，不能写成主键 |
| `scope_profile.steps` | 保留下来的关键 scope 加工步骤 | 还原 SQL 加工链路，每个阶段要带 scope 名称 |
| `business_profile` | 程序生成的业务目标和阶段线索 | 帮 LLM 归纳任务目标，但不能当成人工确认结论 |
| `business_rule_candidates` | WHERE/JOIN/HAVING 条件抽出的规则候选 | 归纳准入、排除、时间窗口、名单命中等规则 |
| `important_columns` | 关键输出字段索引 | 找核心标识、时间、分类、指标、派生字段 |
| `end_to_end_lineage` | ROOT 输出字段到物理字段的端到端血缘 | 判断字段来源和 `trace_complete` |
| `related_metadata` | 输入表/输出表的表级和字段级元数据 | 用中文表名、表说明、字段注释增强语义 |
| `filters_summary` | 重要过滤条件摘要 | 补充规则表达式 |
| `expression_catalog` | CASE/窗口/聚合/表达式摘要 | 解释派生字段逻辑 |
| `diagnostics` | 解析告警和统计 | 输出可信度和风险边界 |
| `compact_policy` | profile 瘦身策略和截断标记 | 判断是否需要查看 `lineage.json` |
| `read_order` | 推荐读取顺序 | 指导 LLM 先看什么、后看什么 |

### 1.5 scope_profile.steps 的结构

`scope_profile.steps` 是 LLM 还原加工链路的核心。每个 step 代表一个有业务意义的
scope，例如 CTE、子查询、UNION 或 ROOT。

典型结构：

```json
{
  "scope_id": "cte:t2",
  "name": "t2",
  "kind": "cte",
  "role": "dedup",
  "operations": ["distinct", "join", "window"],
  "business_summary": "读取提现审批表；关联 1 个上游；使用窗口函数取最新；去重",
  "direct_inputs": ["cte:t1", "dwd.dwd_ap_wdraw_aprv_det_df"],
  "physical_source_tables": ["dwd.dwd_ap_wdraw_aprv_det_df"],
  "output_columns": 6,
  "logic": {
    "joins": [],
    "filters": [],
    "aggregations": [],
    "window_functions": [],
    "case_when": [],
    "key_renames": [],
    "distinct": true,
    "union_branches": 0,
    "lateral_views": []
  }
}
```

`role` 是技术加工角色，不是业务结论。LLM 输出时要翻译成业务动作：

- `filter`：按条件筛选/保留/排除；
- `join`：关联补充/名单命中/维表映射；
- `dedup`：去重名单构造/窗口取最新/避免关联放大；
- `aggregate`：按粒度汇总指标；
- `window`：排序取首/取末/取最新；
- `case_when`：派生分类/状态/标签；
- `union`：合并来源/渠道/策略分支；
- `lateral_view`：展开数组或复杂类型。

### 1.6 related_metadata 的结构

`related_metadata` 分为输入表和输出表：

```json
{
  "related_metadata": {
    "input_tables": {
      "ods.customer_detail": {
        "table_metadata": {
          "table_name_cn": "客户明细表",
          "table_desc": "客户基础属性和状态明细",
          "table_label_layer": "ODS"
        },
        "column_details": [
          {"name": "customer_id", "type": "string", "comment": "客户ID"},
          {"name": "dt", "type": "string", "comment": "日期分区"}
        ],
        "metadata_complete": true
      }
    },
    "output_tables": {
      "mart.customer_profile": {
        "table_metadata": {
          "table_name_cn": "客户画像表",
          "table_desc": "面向分析的客户画像结果表",
          "table_label_layer": "APP"
        },
        "column_details": [
          {"name": "customer_id", "type": "string", "comment": "客户ID"}
        ],
        "metadata_complete": true
      }
    }
  }
}
```

输入表字段不是“只保留血缘中出现的字段”，而是去掉明确未出现在任何 scope 中的字段。
遇到 `SELECT *`、未知引用或 schema 边界时，会保守保留已知字段，避免误删隐式引用字段。

### 1.7 end_to_end_lineage 的结构

`end_to_end_lineage` 描述 ROOT 输出字段到物理字段的追溯：

```json
{
  "column": "customer_id",
  "transform": "DIRECT",
  "expression": "`a`.`customer_id`",
  "trace_complete": true,
  "physical_sources": [
    {
      "table": "ods.customer_detail",
      "column": "customer_id",
      "transform": "DIRECT"
    }
  ]
}
```

如果追溯不完整，会输出：

```json
{
  "column": "x",
  "transform": "UNKNOWN",
  "trace_complete": false,
  "trace_incomplete_reasons": ["star_not_expanded"]
}
```

`trace_complete=false` 不一定表示 SQL 错误，通常表示当前 schema 或静态解析信息不足。

### 1.8 profile.json 的瘦身边界

为了让 LLM 一次读完，`profile.json` 会控制体积。常见标记：

- `large_profile_compaction=true`：触发大文件瘦身；
- `direct_lineage_expressions_omitted`：直接血缘表达式被省略的数量；
- `scope_profile_logic_tightened=true`：scope 中部分 logic 细节被压缩；
- `fields_truncated=true`：规则字段列表被截断；
- `expression_omitted=true`：复杂条件表达式被省略；
- `columns_truncated=true`：表字段元数据展示被截断；
- `sections_truncated=true`：业务 section 被截断。

LLM 生成文档时必须说明这些边界，并提示完整细节看 `lineage.json` 或 `diagnostics.json`。

## 2. 如何生成 profile.json

### 2.1 CLI：解析单个 SQL 文件

安装包后可以使用 `scope-lineage` 命令：

```bash
scope-lineage parse \
  --sql-file /path/to/task.sql \
  --task-name customer_profile_task \
  --out /tmp/scope-output \
  --schema /path/to/schema_info.csv \
  --table-metadata /path/to/tables_metadata.csv \
  --md \
  --html \
  --insight
```

参数说明：

| 参数 | 是否必需 | 说明 |
| --- | --- | --- |
| `--sql-file` | 是 | SQL 文件路径 |
| `--task-name` | 否 | 任务名；不传时使用 SQL 文件名 |
| `--out` | 是 | 输出根目录 |
| `--schema` | 否 | 字段 schema，CSV/JSON |
| `--table-metadata` | 否 | 表级中文名/描述/分层元数据，CSV/JSON |
| `--md` | 否 | 额外输出 `lineage.md` 和 Mermaid 视图 |
| `--html` | 否 | 额外输出离线 `report.html` |

输出目录示例：

```text
/tmp/scope-output/customer_profile_task_0/
  lineage.json
  profile.json
  diagnostics.json
  lineage.md
  report.html
  task_insight.json
  task_insight.html
  views/
```

其中 `profile.json` 就是给 LLM 读取的文件。

如果已有输出目录里已经存在 `lineage.json` 和 `profile.json`，也可以直接补生成任务理解工作台：

```bash
scope-lineage insight \
  --input /tmp/scope-output/customer_profile_task_0
```

### 2.2 批量：解析 task_info 目录

如果任务目录里每个文件都是任务 JSON，格式至少包含 `task_name` 和 `sql`：

```json
{
  "task_name": "customer_profile_task",
  "sql": "INSERT OVERWRITE TABLE mart.customer_profile SELECT ..."
}
```

可以使用批量工具：

```bash
python3 tools/run_scope_corpus.py \
  --input-dir /Users/yinguoliang/dev/sqllineageparse/task_info/test_files \
  --out /tmp/scope-task-output \
  --schema /Users/yinguoliang/dev/sqllineageparse/task_info/test_files/schema_info.csv \
  --table-metadata /path/to/tables_metadata.csv \
  --md \
  --html \
  --insight
```

如果要跑默认 `task_info` 目录下的所有子目录：

```bash
python3 tools/run_scope_corpus.py \
  --out /tmp/scope-task-output \
  --schema /path/to/schema_info.csv \
  --table-metadata /path/to/tables_metadata.csv \
  --md \
  --html
```

如果只跑默认 `task_info` 下的某一个子目录：

```bash
python3 tools/run_scope_corpus.py \
  --dir 客服任务列表 \
  --out /tmp/scope-task-output \
  --schema /path/to/schema_info.csv \
  --table-metadata /path/to/tables_metadata.csv \
  --insight
```

批量输出结构：

```text
/tmp/scope-task-output/
  errors.json
  客服任务列表/
    task_a/
      profile.json
      lineage.json
      diagnostics.json
    task_b/
      profile.json
      lineage.json
      diagnostics.json
```

### 2.3 Python：直接调用解析 API

如果在程序里调用，可以使用：

```python
from pathlib import Path

from lineage_parser import (
    attach_table_metadata,
    load_schema,
    load_table_metadata,
    parse_all_scope_lineage,
    to_profile_json,
    write_output,
)

sql = Path("/path/to/task.sql").read_text(encoding="utf-8")

schema = load_schema("/path/to/schema_info.csv")
schema = attach_table_metadata(schema, load_table_metadata("/path/to/tables_metadata.csv"))

results = parse_all_scope_lineage(
    sql,
    task_name="customer_profile_task",
    schema=schema,
)

for result in results:
    out_dir = Path("/tmp/scope-output") / result.task_id.replace("#", "_")
    write_output(result, out_dir)

    profile_text = to_profile_json(result)
    print(profile_text[:1000])
```

常用 API：

| 方法 | 作用 |
| --- | --- |
| `load_schema(path)` | 读取字段 schema，返回解析器可用的 schema map |
| `load_table_metadata(path)` | 读取表级元数据 |
| `attach_table_metadata(schema, table_metadata)` | 把表级元数据挂到 schema 上 |
| `parse_all_scope_lineage(sql, task_name, schema=schema)` | 解析 SQL，返回一个或多个 `ScopeLineageResult` |
| `to_profile_dict(result)` | 返回 Python dict 形式的 `profile.json` |
| `to_profile_json(result)` | 返回 JSON 字符串 |
| `write_output(result, out_dir)` | 写出 `lineage.json`、`profile.json`、`diagnostics.json` |

### 2.4 生成后建议先审计

生成 profile 后建议跑审计，先判断是否有 UNKNOWN、断链、星号未展开或 schema 边界：

```bash
python3 tools/audit_scope_output.py \
  --out-dir /tmp/scope-task-output/客服任务列表 \
  --report /tmp/scope-audit.md \
  --json /tmp/scope-audit.json \
  --fail-on-red
```

审计等级：

- `RED`：结构性问题，优先修复；
- `YELLOW`：可用但存在 schema/SQL 歧义或准确性边界；
- `GREEN`：未发现结构问题或明显告警。

## 3. 如何使用 profile.json 生成 Markdown 文档

### 3.1 推荐读取顺序

把 `profile.json` 提供给 LLM 后，推荐让 LLM 按以下顺序读取：

1. `summary`
2. `business_profile`
3. `grain`
4. `scope_profile.steps`
5. `business_rule_candidates`
6. `important_columns`
7. `end_to_end_lineage`
8. `related_metadata`
9. `diagnostics`
10. `compact_policy`

这个顺序的目的：

- 先理解任务规模和目标；
- 再看业务阶段和 scope 加工链路；
- 再看规则、字段、血缘；
- 最后判断可信度和瘦身边界。

### 3.2 推荐输出模式

固定使用两种模式。

摘要模式用于检索、预览、知识库卡片，通常 800-1500 字：

- 任务定位；
- 输入输出；
- 核心加工逻辑；
- 核心字段/规则；
- 可信度和风险。

详细还原模式用于数据治理、交接、代码评审、业务规则核对：

- L1：任务概览；
- L2：输入输出；
- L3：加工步骤；
- L3.5：业务规则/判断逻辑；
- L4：核心字段/指标；
- L5：血缘可信度和风险边界。

### 3.3 详细还原模式的硬性要求

为了避免 LLM 越写越省，详细还原模式必须满足：

- 输入表和输出表必须同时写英文表名、中文表名/表说明；
- 核心字段必须尽量同时写英文名、中文名/注释、在本任务中的作用；
- 每个加工阶段必须绑定 `scope_profile.steps[].name`；
- 每个加工阶段必须写关键条件表达式或条件摘要，不能只列字段；
- 每条业务规则必须写：规则名称、所在 scope、条件、字段语义、字段在规则中的作用、处理结果；
- `grain.keys` 只能写成候选输出标识字段，不能写成主键；
- `trace_complete=false` 必须进入风险边界；
- `compact_policy` 中的截断、省略和瘦身必须说明；
- 不能编造 profile 中不存在的业务口径。

### 3.4 可直接使用的 LLM Prompt

项目内已经维护了 Prompt 模板：

- `docs/llm-profile-prompt.zh-CN.md`
- `docs/llm-profile-guide.zh-CN.md`

调用 LLM 时可以使用下面的简化版：

```text
你是数据仓库 SQL 任务画像分析助手。
请只基于给定 profile.json 生成任务分级结构，不要编造 profile 中不存在的业务事实。

生成模式：详细还原模式。

读取顺序：
1. summary
2. business_profile
3. grain
4. scope_profile.steps
5. business_rule_candidates
6. important_columns
7. end_to_end_lineage
8. related_metadata
9. diagnostics
10. compact_policy

输出结构：
L1：任务概览
L2：输入输出
L3：加工步骤
L3.5：业务规则/判断逻辑
L4：核心字段/指标
L5：血缘可信度和风险边界

要求：
- 输入表/输出表必须保留英文表名和中文表名/说明。
- 每个加工阶段必须写 scope 名称、scope 类型、技术角色、业务动作、输入、关键条件、关键字段关系、输出作用。
- 每条规则必须写所在 scope、条件表达式或摘要、字段中文语义、字段在规则中的判断作用、处理结果。
- 不要把 grain.keys 写成主键，只能写候选输出标识字段。
- trace_complete=false 的字段必须放入风险边界。
- schema 缺失、SELECT * 未展开、expression_omitted、fields_truncated、large_profile_compaction 等边界必须说明。
- 输出 Markdown，不要输出 JSON。

profile.json:
```json
{{PROFILE_JSON}}
```
```

### 3.5 生成文档后的校验

生成 Markdown 后，可以用验证器做结构和事实边界检查：

```bash
python3 tools/validate_llm_profile_doc.py \
  --profile /tmp/scope-output/customer_profile_task_0/profile.json \
  --doc /tmp/customer_profile_task.md \
  --json /tmp/customer_profile_task.validation.json
```

验证器会检查：

- 是否包含 L1-L5；
- 是否写出 `task_name` 和 `target_table`；
- 是否把 `grain.keys` 正确描述为候选标识字段；
- 是否披露 `trace_complete=false` 字段；
- 是否说明 schema/star/metadata 边界；
- 是否使用了表中文名和字段注释；
- 是否说明 profile 截断或瘦身边界。

校验成功示例：

```text
Profile doc validation: ok=True, errors=0, warnings=0
```

### 3.6 常见问题

#### profile.json 里没有完整 SQL 条件怎么办？

看 `business_rule_candidates` 是否有：

- `expression_omitted=true`
- `fields_truncated=true`
- `field_details_truncated=true`

如果有，Markdown 中要写：

```text
该规则在 profile 中已瘦身，只能还原主要字段和条件摘要；完整布尔表达式需要查看 lineage.json。
```

#### 输出字段都 trace_complete=true，是否代表完全正确？

不是。它表示“从当前解析结果看可完整追溯到物理字段”。业务口径是否正确仍需要人工或业务规则核对。

#### 表中文名或字段中文名缺失怎么办？

不能编造。应写：

```text
中文名缺失，只能根据英文表名/字段名/SQL 表达式推断。
```

#### 大 profile 怎么处理？

如果 `compact_policy.large_profile_compaction=true`，说明 profile 已主动瘦身。
LLM 文档要优先保留：

1. 任务目标；
2. 输入输出表语义；
3. 关键 scope 加工链路；
4. 关键规则条件；
5. 核心字段和指标；
6. `trace_complete` 和瘦身边界。

不要试图在 Markdown 中穷举所有字段和所有中间 scope。完整机器明细以 `lineage.json` 为准。
