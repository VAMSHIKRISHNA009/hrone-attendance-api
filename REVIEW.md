# REVIEW.md

| # | Where | What is wrong | How you'd notice it | How you fixed it |
|---|---|---|---|---|
| 1 | `compute_late_minutes` | It uses naive/local datetimes and does not truncate instants to whole seconds. | A timestamp with milliseconds or a non-UTC host changes the result. | Normalize epoch milliseconds to UTC and truncate before calculation. |
| 2 | `compute_late_minutes` | It does not handle overnight attendance dates. | A 04:00 punch for a 22:00–06:00 shift is assigned to the wrong day. | Added IST attendance-date and shift-boundary helpers. |
| 3 | `compute_work_hours` | Python float rounding is not the required half-up rounding. | A value exactly on a half-cent can round differently. | Use `Decimal` with `ROUND_HALF_UP`. |
| 4 | `compute_overtime` | It constructs a shift end from the calendar date without considering overnight shifts. | An overnight punch-out is treated as many hours late or as negative overtime. | Compute shift end from the punch-in and employee shift. |
| 5 | `create_employee` | Duplicate protection is only a pre-check, so concurrent creates can both pass. | Parallel identical POSTs can create duplicates. | Create a unique `emp_code` index and catch `DuplicateKeyError`. |
| 6 | `create_employee` | It returns a BSON `datetime` instead of epoch milliseconds. | The response does not match the OpenAPI schema. | Convert instants at the API boundary. |
| 7 | `list_employees` | `skip` is `page * page_size`, so page 1 skips the first page. | Page 1 starts at item 21. | Use `(page - 1) * page_size`. |
| 8 | `list_employees` | `total` ignores the department filter. | A filtered response reports the collection total. | Count the filtered query. |
| 9 | `list_employees` | There is no deterministic sort or page-size validation. | Page order changes after inserts; oversized pages are accepted. | Sort by `emp_code` and constrain pagination with FastAPI validation. |
| 10 | `punch_in` | It does not return 404 for an unknown employee before dereferencing the employee. | An unknown employee causes a 500/KeyError. | Check the employee and return 404. |
| 11 | `punch_in` | It uses check-then-insert for the natural key. | Two simultaneous requests can both receive 201. | Use a unique compound attendance index and handle duplicate-key conflicts. |
| 12 | `punch_in` | It uses local naive timestamps and the wrong overnight date rule. | UTC/IST boundary and overnight cases fail. | Normalize epoch milliseconds and calculate the IST shift-start date. |
| 13 | `list_attendance` | It loads the entire matching collection and sorts/slices in Python. | Memory and latency grow with 100,000 records. | Push filter, sort, skip, limit, and count to MongoDB with indexes. |
| 14 | `list_attendance` | It mutates Mongo documents with `pop("_id")` and leaks BSON datetimes. | Reusing a document or serializing it produces inconsistent output. | Build response values without mutating stored documents and convert dates. |
| 15 | `list_attendance` | It has no date/status validation and uses inconsistent ordering. | Bad dates can return 200; ties have unstable order. | Validate query values and sort by date descending, then `emp_code` ascending. |

I also checked the starter's `health` shape and basic employee/punch response field names. Their happy-path shapes were reasonable, but the readiness check and all response serialization needed to be made MongoDB-aware and contract-compliant.
