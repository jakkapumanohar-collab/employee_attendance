# REVIEW.md

| # | Where | What is wrong | How to notice | Fix |
|---|---|---|---|---|
| 1 | `compute_late_minutes` | Uses a same-day shift start without overnight handling and relies on unvalidated times. | Test overnight and malformed shift values. | Validate shift times and calculate shift bounds in IST. |
| 2 | `compute_work_hours` | Python `round` does not guarantee the required decimal half-up rule. | Test a duration at a rounding midpoint. | Use `Decimal` with `ROUND_HALF_UP`. |
| 3 | `compute_overtime` | Uses the wrong shift end for overnight shifts and does not enforce the 30-minute threshold. | Test 29:59 and 30:00 overtime. | Calculate the correct shift end and threshold. |
| 4 | `/health` | Always returns success without checking MongoDB. | Stop MongoDB and call `/health`. | Run `ping`, return 503 on database failure. |
| 5 | `create_employee` | Duplicate check followed by insert races; no unique index. | Send simultaneous identical employee creates. | Unique index and catch `DuplicateKeyError`. |
| 6 | `create_employee` | Does not validate date or shift-time formats. | Send invalid `joined_on` or `shift_start`. | Validate ISO dates and `HH:MM`. |
| 7 | `list_employees` | Pagination skips `page * page_size`, so page 1 skips records; total ignores department filter. | Compare page 1 and filtered total. | Use `(page-1)*page_size` and count the filtered query. |
| 8 | `list_employees` | Results have no deterministic required sort. | Repeat paginated requests. | Sort by `emp_code`. |
| 9 | `punch_in` | Does not check that employee exists before dereferencing it. | Punch in an unknown code. | Return 404. |
| 10 | `punch_in` | Uses local machine time and treats milliseconds as naive local time. | Run under a non-IST host timezone. | Convert epoch milliseconds to UTC and derive IST calendar date. |
| 11 | `punch_in` | No unique `(emp_code,date)` index, so simultaneous requests can both insert. | Send parallel punch-ins. | Unique compound index and handle duplicate key as 409. |
| 12 | `punch_in` | Accepts arbitrary status values and does not truncate timestamps to seconds. | Send invalid status or fractional-second timestamp. | Restrict statuses and truncate instants. |
| 13 | `/attendance` | Loads all matching records into Python, then sorts and paginates; too slow at scale. | Test against 100k records. | Count separately and sort/skip/limit in MongoDB. |
| 14 | `/attendance` | Returns `_id` and BSON datetime values inconsistently; pagination limits are unchecked. | Inspect response JSON and use page_size > 100. | Convert timestamps, omit `_id`, validate pagination. |
| 15 | Missing punch-out | No endpoint exists. | Call the contracted endpoint. | Implement atomic update and derived calculations. |
| 16 | Missing correction | No regularization or audit trail exists. | PATCH a record. | Recompute fields and atomically append history. |
| 17 | Missing analytics | Required aggregate endpoints are absent. | Call each contracted analytics endpoint. | Implement MongoDB aggregation pipelines. |
| 18 | Missing explain | No index-plan endpoint exists. | Call `/admin/explain/...`. | Return executionStats explain output. |

Not considered defects: using `emp_code` as the business identifier is correct; attendance date is a string by design; missing legacy `history` and `half_day` fields must be treated as empty/false.
