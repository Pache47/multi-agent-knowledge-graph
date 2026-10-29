# Sample evidence

All records are fictional and labelled as simulated; they are not real maintenance
guidance. The main incident happens on **14 October 2026** in plant time
(Asia/Singapore, UTC+08:00). The technician's first question is at 08:20.

## The shared folder

[shared_folder/](shared_folder) stands in for the customer's drive. Its
[manifest](shared_folder/manifest.json) lists each file's name and the time it was
added, nothing about its content. To add a file, copy it in and add one manifest line;
no code change is needed.

| ID | File | What makes it messy |
|---|---|---|
| E1 | equipment_notes.txt | Free-text walkdown: aliases, two configurations, components listed under their parent |
| E2 | morning_export.log | Two timestamp formats; one event exported twice |
| E3 | shift_handover.txt | Observations mixed with an operator's guess; vague "outfeed" |
| E4 | maintenance_history.txt | Jobs on different machines; "CV01" without its hyphen; old configuration |
| E5, E6 | manuals/*.pdf | Similar titles; only one matches CV-01's installed configuration |
| E7a | approved_parts.txt | Compatibility, including a non-substitution statement |
| E7b | stores_snapshot.txt | Aligned columns, yesterday's counts, no reservations |
| E8 | site_maintenance_boundaries.txt | What the assistant may and may not advise |
| N1–N3 | friday_closeout.txt, fitter_notebook.md, unassigned_callout.txt | An email, a notebook alias, an unidentifiable machine |
| C3 | stores_export_16oct.csv | New reserved-quantity column (needs schema review) |
| R1 | filling_equipment.csv | Equipment register for a second area |
| N4 | work_orders_october.pdf | Two-page PDF, two pumps, blank fields, unrelated note |

Files added after 08:20 on 14 October (N1–N4, C3, R1) stay outside the original
incident view but are available for later questions.

The PDFs can be regenerated with `tools/build_sample_manuals.py` and
`tools/build_development_work_orders.py`. PDF reading needs selectable text (at most 20
pages, 2 MB, 100,000 characters); scans are held for OCR review.

## Other folders

[scenarios/sensor_stop/](scenarios/sensor_stop) is a separate line (a carton buffer with
detector stops) in the same manifest format. It is built by the same pipeline and never
merged into the main graph.

## Kept outside every agent's input

`evaluation/` (gold checks, questions, later outcomes), `docs/` and `tools/`. Only files
listed in a manifest are read.
