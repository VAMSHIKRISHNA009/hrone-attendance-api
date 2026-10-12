# DECISIONS.md

1. **Indexes.** I create a unique `employees.emp_code` index and a unique `(emp_code, date)` attendance index. The latter is both the natural-key guarantee and the punch-in race solution. Attendance also has `(date desc, emp_code asc)` for listing, `(emp_code, date desc)` for employee lookups, and date/status and punch-in indexes for analytics and punch-out. Employee department/code and joined/department indexes support filtered lists and headcount joins.

2. **Punch-in race.** Both requests may read the employee, but neither performs a correctness check before writing. The unique attendance index lets MongoDB accept one insert; the other gets `DuplicateKeyError`, which becomes 409.

3. **Ties.** I use `$setWindowFields` with `$rank`, then filter `rank <= limit`. Ties share a rank and the next rank is skipped, so a cutoff tie returns every tied row.

4. **Headcount.** Department summary starts from employees, filters by `joined_on <= month end`, and uses a preserving lookup/unwind. Employees with no logs still contribute one headcount row.

5. **100x data.** I would precompute daily facts or materialized monthly summaries and move trend analytics to a reporting collection, while retaining the raw audit records as the source of truth.
