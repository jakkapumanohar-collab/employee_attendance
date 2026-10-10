# DECISIONS.md

**Indexes.** I use a unique index on `employees.emp_code` and a unique compound index on attendance `(emp_code, date)` to enforce identity and one record per employee/day under concurrency. Department/code and joined-date indexes support employee listing and headcount. Date/employee and status/date indexes support attendance filters and ordered lists. I would reconsider extra overlapping indexes after measuring write cost and explain plans.

**Punch-in race.** Both requests can initially see no record, but the unique `(emp_code, date)` index permits only one insert. The winner gets 201; the loser gets a duplicate-key error translated to 409. An application-level pre-check alone would not be safe.

**Ties.** The leaderboard uses MongoDB `$rank`, so equal late-minute totals share a rank and the next rank is skipped. Rows with equal ranks are then ordered by employee code. The limit is applied after ranking.

**Headcount.** The department summary starts from employees joined on or before the month end and uses `$lookup` with `preserveNullAndEmptyArrays`, so employees with no attendance logs still contribute to headcount.

**100x scale.** I would inspect explain plans and production query metrics, tune compound indexes against real filters, add load tests, and consider pre-aggregated monthly summaries if analytics became expensive.
