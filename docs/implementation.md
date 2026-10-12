# Notion meeting update extractor

Python 3.10+, standard library only. Run on demand:

```sh
cd ~/code/robotics/notion-update-extractor
python3 notion_feed.py
```

Edit `.env` to set the integration token, source Tasks/Projects IDs, existing Updates database/data source IDs, and timezone. Keep `.env` private. The integration needs read access to sources and insert/update access to Updates. No scheduler is installed.

Each successful run prints only the newly created Notion summary URL to stdout. Progress goes to stderr. The output is one page inside Updates titled `Update generated on YYYY-MM-DD HH:MM:SS America/Chicago` with all weekly task updates visible while scrolling. New tasks show objectives and deliverables expanded; existing tasks keep those details in a dropdown. All task properties are also available in a dropdown. Plain task headings have a page mention underneath. Key properties appear in a linked row.

The window starts after the previous Sunday and includes today; a Sunday run covers Monday through that Sunday. Newly created tasks without updates are included. All updates for a task stay together. Tasks are grouped first by the assigned subteam of their project, then by project. People overlap orders tasks inside each project. For tasks linked to multiple projects, the alphabetically first project determines placement; all project links remain in the task row. Multi-subteam projects use one combined subteam heading. Plain-text participant names are included; edit `people_map.json` to add aliases.

Existing suitably scoped synced blocks are referenced live. Otherwise the renderer copies native text, headings, lists, tables, images, videos, audio, PDFs, files, embeds, bookmarks, equations and callouts, retaining rich-text annotations and nesting. Only buttons are omitted. Blocks the API cannot recreate (including child pages/databases and unknown block types) retain a source link and any readable children. Text containers such as quotes, code, toggles and columns retain their structure. Media up to 20 MB each is copied as new attachments for the summary; larger media retains a stable link to the original source block, not embedded using expiring download URLs. Copied sections are snapshots; rerun for later changes. Synced sections reflect later edits inside that section, but newly added tasks/sections need a rerun. Native Notion database views cannot be recreated as inline rows; the script creates a normal linked table with property values.

## Write boundary

Source Tasks/Projects are read-only. All page/block/schema mutations are guarded to the configured, verified Updates database. Relation property writes are forbidden to avoid reciprocal source changes. The script never creates or converts synced blocks in source pages, and refuses mutations through synced references. File-import API calls only prepare new media attachments for an Updates page; they do not modify source files. Existing old per-update rows are not removed.

The destination gains Generated at, Window, Tasks, Updates, and State properties. A page is marked Building until all blocks are written, then Ready. On failure a partial Building page can remain, with its URL in stderr; a subsequent run creates a new page. No automatic create retries that could duplicate pages.

```sh
python3 notion_feed.py --dry-run    # fresh scan and local preview, no Notion writes
python3 notion_feed.py --from-scan  # explicitly reuse prior local scan, possibly stale
python3 -m unittest -q
```

Local `.cache/tasks.json`, `preview.json` and `meeting-preview.json` contain workspace content; keep them private. No AI service receives task content from this standalone script. Source links remain available under each task heading.
