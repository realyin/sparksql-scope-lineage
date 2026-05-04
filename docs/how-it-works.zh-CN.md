# 原理说明

中文 | [English](how-it-works.md)

这篇文档说明 Scope Lineage 的几个核心设计取舍。

## 1. 先保留 Scope，再追物理血缘

解析器不会一上来就把目标字段直接展开到物理表字段。它会先构建一张 scope 图。

例如：

```sql
INSERT OVERWRITE TABLE mart.orders
WITH cleaned AS (
  SELECT order_id, user_id FROM ods.orders WHERE deleted = false
)
SELECT order_id, user_id FROM cleaned
```

图里会保留这条链路：

```text
ods.orders -> cte:cleaned -> ROOT
```

`ROOT.order_id` 指向 `cte:cleaned.order_id`，`cte:cleaned.order_id` 再指向 `ods.orders.order_id`。

只有在需要看物理血缘时，视图层才会沿着这张图继续向上追。

## 2. 稳定的 Scope ID

每个查询块都会拿到一个稳定的 id：

| SQL 结构 | Scope id |
| --- | --- |
| 写入目标表的 SELECT | `ROOT` |
| CTE | `cte:<name>` |
| 子查询 | `subq:<alias>` |
| UNION 容器 | `union:<context>` |
| UNION 分支 | `union:<context>:bNN` |
| UDTF / lateral 输出 | `udtf:<alias>` |

如果嵌套查询里出现同名别名，会自动加后缀去重，例如 `subq:a_2`。

## 3. 字段解析

对于每个 SELECT 类 scope，解析器会遍历投影列表，生成 `ScopeColumn`。

每个输出字段会记录：

- 字段名；
- 转换类型；
- 原始表达式；
- 直接上游来源；
- CASE、窗口函数、聚合、UNION、MERGE 等结构的附加信息。

常见转换类型：

| 类型 | 含义 |
| --- | --- |
| `DIRECT` | 字段直接透传 |
| `EXPRESSION` | 普通计算表达式 |
| `CONDITIONAL` | CASE / IF |
| `AGGREGATE` | 聚合函数 |
| `WINDOW` | 窗口函数 |
| `CONSTANT` | 常量或无输入表达式 |
| `UNION` | UNION 按位置对齐 |
| `EXPAND_ALL` | 尚未展开的星号 |

## 4. 带限定和不带限定的字段

像 `s.user_id` 这种带表别名的字段，会通过当前 scope 的 source 去解析。

像 `user_id` 这种不带表别名的字段，会更保守地处理：

1. 先查上游 scope 是否有同名输出字段；
2. 如果提供了 schema，再查物理表里是否有同名字段；
3. 在 SQL 结构足够明确时，走少量兜底规则；
4. 仍然无法可靠判断时，标为 `UNKNOWN` 并写入 diagnostics。

这里的 diagnostics 是有意保留的。静态解析宁可把不确定性暴露出来，也不要悄悄猜一个看似合理的来源。

## 5. UNION 按位置对齐

UNION 分支按位置对齐，不按字段名对齐。

```sql
SELECT id, name FROM a
UNION ALL
SELECT user_id, full_name FROM b
```

合成的 `union:*` scope 会采用第一个分支的字段名，也就是 `id`、`name`，同时保留每个分支的来源信息，方便检查每个位置来自哪里。

## 6. SELECT * 的处理

`SELECT *` 有三种结果：

1. 如果上游 scope 已经有明确字段，就从上游 scope 展开；
2. 如果提供了物理表 schema，就从 schema 展开；
3. 如果两者都没有，就保留 `EXPAND_ALL` 占位，并产生 warning。

解析器也会把下游明确引用到的字段补出来。但如果希望完整覆盖 `*` 里的所有字段，仍然需要提供 schema 元数据。

## 7. MERGE 的处理

MERGE 的 UPDATE 和 INSERT 会被表示成 ROOT 字段，并带上分支信息：

- `matched`：来自 `WHEN MATCHED THEN UPDATE`
- `not_matched`：来自 `WHEN NOT MATCHED THEN INSERT`

MERGE DELETE 是行级行为，不对应目标字段的写入来源。因此它会作为 diagnostic 报出，不生成目标字段血缘。

## 8. 视图和审计

解析结果和视图是分开的：

- `scope_overview.mmd`：scope 级别 DAG；
- `field_lineage.mmd`：字段级图；
- `physical.mmd`：物理表字段到 ROOT 字段；
- `per_column/*.mmd`：单个目标字段的追踪图。

审计工具读取已经生成的输出，不重新解析 SQL。这样验证步骤和解析入口是分开的，更适合做回归检查。

## 9. 为什么 diagnostics 很重要

静态 SQL 血缘天然会遇到不确定性：

- schema 可能缺失；
- 字段可能没有表别名；
- 某些方言语法只支持了一部分；
- 运行时行为不一定完整体现在 SQL 文本里。

Scope Lineage 会把这些情况作为 diagnostics 暴露出来。一个边界清楚的部分结果，通常比一个看起来很完整但悄悄猜测的结果更有用。

