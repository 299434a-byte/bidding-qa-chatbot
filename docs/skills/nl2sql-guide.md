# NL2SQL 字段与口径说明

用 SQL 回答统计类问题（数量、金额合计、排名、时间筛选）时，先读本技能确认表名、字段与口径，再生成 SQL。

## 表与常用字段

- `bidding_notices`（招标公告）：project_code、project_name、budget_amount（**万元**）、purchaser、agency、publish_date、bid_deadline、project_nature。
- `bidding_project`（中标 / 项目结果）：project_code、project_name、winning_bidder（中标人）、winning_amount（中标金额，**万元**）、award_date（中标日期）。
- `companies`（企业）：company_name、unified_credit_code（统一社会信用代码）、registered_capital（注册资本）、address。

## 口径规则

1. **金额单位一律万元**。合计写 `SUM(budget_amount)`，结果直接是万元；不要乘 10000。
2. **跨表统计用 project_code 关联**（bidding_notices ↔ bidding_project），company_name 只作辅助核对，不要用公司名当唯一关联键（存在同名）。
3. **时间过滤用 publish_date**（公告口径）或 award_date（中标口径），比较用字符串日期 `'YYYY-MM-DD'` 即可（列为 DATE 类型）。
4. **模糊匹配**公司名 / 项目名用 `LIKE '%关键词%'`；统计去重用 `COUNT(DISTINCT project_code)`，防止一个项目多包重复计数。
5. **空值处理**：budget_amount 可能为 NULL，合计时用 `COALESCE(budget_amount, 0)`；回答时若存在 NULL 要说明「另有 N 条未披露金额」。
6. 生成的 SQL 只读（SELECT）；口径拿不准时先在回答里注明所用口径，再给结果。
