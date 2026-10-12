"""Create an on-demand, deterministic meeting agenda; stdout is its Notion URL."""
import argparse
from contextlib import redirect_stdout
import datetime as dt
import fcntl
import json
import os
from pathlib import Path
import re
import sys
from zoneinfo import ZoneInfo

import notion_feed as n
import notion_blocks as nb


def text(value, url=None):
    result = n.rich(str(value))
    if url:
        for item in result:
            item['text']['link'] = {'url': url}
    return result


def paragraph(value):
    return {'object': 'block', 'type': 'paragraph', 'paragraph': {'rich_text': text(value)}}


def heading(value, level=2, url=None):
    kind = f'heading_{level}'
    return {'object': 'block', 'type': kind, kind: {'rich_text': text(value)}}


def source_url(task, block_id=None):
    return task['url'].split('#')[0] + ('#' + block_id.replace('-', '') if block_id else '')


def join_rich(values, separator=', '):
    result = []
    for index, value in enumerate(values):
        if index:
            result += text(separator)
        result += value
    return result or text('—')


def table(headers, rows):
    def row(cells):
        return {'object': 'block', 'type': 'table_row', 'table_row': {'cells': cells}}
    return {'object': 'block', 'type': 'table', 'table': {
        'table_width': len(headers), 'has_column_header': True, 'has_row_header': False,
        'children': [row([text(h) for h in headers])] + [row(cells) for cells in rows]}}


def toggle(label, children):
    return {'object': 'block', 'type': 'toggle', 'toggle': {'rich_text': text(label), 'children': children or [paragraph('Not specified.')]}}


def normalized_name(name):
    return ' '.join(name.casefold().strip().lstrip('@').split())


class Resolver:
    def __init__(self, api, snapshots, config):
        self.api = api
        self.pages = {entry['task']['id']: entry['task'] for entry in snapshots}
        if config.get('projects_data_source_id'):
            for page in api.query(config['projects_data_source_id']):
                self.pages[page['id']] = page
        self.aliases = {normalized_name(k): v for k, v in config.get('people_map', {}).items()}
        self.names = {v: k for k, v in config.get('people_map', {}).items()}

    def page(self, ident):
        if ident not in self.pages:
            try:
                self.pages[ident] = self.api.call('GET', 'pages/' + ident)
            except RuntimeError as error:
                if 'HTTP 403' not in str(error) and 'HTTP 404' not in str(error):
                    raise
                return text('Linked page', 'https://www.notion.so/' + ident.replace('-', ''))
        page = self.pages[ident]
        return text(n.title(page), page['url'])

    def person(self, person):
        ident = person['id']
        self.names[ident] = person.get('name') or self.names.get(ident, ident)
        return [{'type': 'mention', 'mention': {'type': 'user', 'user': {'object': 'user', 'id': ident}}}]

    def property(self, prop):
        kind = prop.get('type', '')
        value = prop.get(kind)
        if kind in ('title', 'rich_text'):
            return text(n.plain(value or [])) or text('—')
        if kind in ('select', 'status'):
            return text(value['name'] if value else '—')
        if kind == 'multi_select':
            return text(', '.join(v['name'] for v in value) or '—')
        if kind == 'people':
            return join_rich([self.person(v) for v in value])
        if kind == 'relation':
            return join_rich([self.page(v['id']) for v in value])
        if kind == 'date':
            return text((value['start'] + (' → ' + value['end'] if value.get('end') else '')) if value else '—')
        if kind == 'formula':
            return self.property(value or {})
        if kind == 'rollup':
            if value and value.get('type') == 'array':
                return join_rich([self.property(v) for v in value['array']])
            return self.property(value or {})
        if kind == 'files':
            return join_rich([text(v.get('name', 'Attachment')) for v in value])
        if kind in ('created_by', 'last_edited_by') and value:
            return self.person(value)
        if kind == 'unique_id' and value:
            return text(str(value.get('prefix') or '') + str(value.get('number', '')))
        if kind == 'url' and value:
            return text(value, value)
        if kind == 'checkbox':
            return text('Yes' if value else 'No')
        if value is None:
            return text('—')
        return text(value if isinstance(value, (str, int, float)) else json.dumps(value, ensure_ascii=False))


def extract_context(blocks):
    sections = []
    active = None
    names = re.compile(r'^(objective|objectives|goal|goals|deliverable|deliverables)\s*:?$', re.I)
    for block in n.flatten(blocks):
        kind = block['type']
        title = n.block_text(block).strip()
        if kind.startswith('heading_'):
            level = int(kind[-1])
            if names.fullmatch(title):
                active = {'title': title.rstrip(':'), 'id': block['id'], 'level': level, 'blocks': []}
                sections.append(active)
                continue
            if active and level <= active['level']:
                active = None
        if active:
            active['blocks'].append(block)
    return sections



def plain_participants(updates):
    names = set()
    for update in updates:
        for raw in update['unmapped']:
            for name in re.split(r'[,;/\n]|\s+&\s+|\s+and\s+', raw):
                name = name.strip()
                if name and not re.fullmatch(r'\d{4}-\d{2}-\d{2}', name):
                    names.add(name)
    return sorted(names)


def select_tasks(snapshots, records, config, now=None):
    cutoff, today = n.date_window(config)
    now = now or dt.datetime.now(dt.timezone.utc)
    by_task = {}
    for record in records:
        by_task.setdefault(record['task_id'], []).append(record)
    chosen = []
    for snapshot in snapshots:
        task = snapshot['task']
        created_raw = task.get('created_time')
        created = dt.datetime.fromisoformat(created_raw.replace('Z', '+00:00')) if created_raw else None
        new = bool(created and created <= now and cutoff < created.astimezone(ZoneInfo(config.get('timezone', 'America/Chicago'))).date() <= today)
        updates = by_task.get(task['id'], [])
        if not updates and not new:
            continue
        item = {**snapshot, 'updates': sorted(updates, key=lambda r: (r['date'], r['key'])), 'new': new}
        item['projects'] = {r['id'] for r in task['properties'].get(config.get('project_property', 'Project'), {}).get('relation', [])}
        sub = task['properties'].get('Subteam', {})
        item['subteams'] = {v['name'] for v in sub.get('multi_select', [])}
        if sub.get('select'):
            item['subteams'].add(sub['select']['name'])
        aliases = {normalized_name(k): v for k, v in config.get('people_map', {}).items()}
        item['people'] = {p['id'] for prop in task['properties'].values() if prop.get('type') == 'people' for p in prop.get('people', [])}
        for update in updates:
            item['people'].update(update['people'])
        item['people'].update(aliases.get(normalized_name(name), 'name:' + normalized_name(name)) for name in plain_participants(updates))
        chosen.append(item)
    return chosen


def jaccard(a, b):
    return len(a & b) / len(a | b) if a or b else 0


def order_tasks(items, weights=(100, 20, 5)):
    """Multi-start greedy path followed by 2-opt; deterministic, not a global optimum."""
    items = sorted(items, key=lambda x: (n.title(x['task']).casefold(), x['task']['id']))
    size = len(items)
    if size < 2:
        return items
    def affinity(a, b):
        project = bool(a['projects'] & b['projects']) + .1 * jaccard(a['projects'], b['projects'])
        return weights[0] * project + weights[1] * jaccard(a['subteams'], b['subteams']) + weights[2] * jaccard(a['people'], b['people'])
    scores = [[affinity(a, b) for b in items] for a in items]
    def score(path):
        return sum(scores[a][b] for a, b in zip(path, path[1:]))
    best = None
    for start in range(size):
        path, remaining = [start], set(range(size)) - {start}
        while remaining:
            nxt = max(remaining, key=lambda index: (scores[path[-1]][index], -index))
            path.append(nxt)
            remaining.remove(nxt)
        if best is None or score(path) > score(best):
            best = path
    for _ in range(20):
        improved = False
        for i in range(size - 1):
            for j in range(i + 1, size):
                before = (scores[best[i-1]][best[i]] if i else 0) + (scores[best[j]][best[j+1]] if j+1 < size else 0)
                after = (scores[best[i-1]][best[j]] if i else 0) + (scores[best[i]][best[j+1]] if j+1 < size else 0)
                if after > before + 1e-9:
                    best[i:j+1] = reversed(best[i:j+1])
                    improved = True
        if not improved:
            break
    return [items[i] for i in best]


def organize(items, resolver):
    """Hard subteam/project groups; choose one primary project to avoid task duplication."""
    groups = {}
    for item in items:
        project = min(item['projects'], key=lambda p: (n.plain(resolver.page(p)).casefold(), p)) if item['projects'] else None
        page = resolver.pages.get(project, {})
        teams = set()
        for name, prop in page.get('properties', {}).items():
            if 'subteam' in name.casefold():
                if prop.get('type') == 'multi_select':
                    teams.update(v['name'] for v in prop['multi_select'])
                elif prop.get('type') == 'select' and prop.get('select'):
                    teams.add(prop['select']['name'])
        item['meeting_project'] = (project,) if project else ()
        item['meeting_subteams'] = tuple(sorted(teams))
        key = (item['meeting_subteams'], n.plain(resolver.page(project)).casefold() if project else '', project or '')
        groups.setdefault(key, []).append(item)
    return [item for key in sorted(groups) for item in order_tasks(groups[key], (0, 0, 5))]


def live_regions(items, config):
    regions, missing = {}, []
    for item in items:
        owners = {}
        dates_per_owner = {}
        button_owners = set()
        def walk(blocks, owner=None):
            for block in blocks:
                current = owner
                if block['type'] == 'synced_block':
                    original = block['synced_block'].get('synced_from')
                    current = original['block_id'] if original else block['id']
                if current and nb.is_button(block):
                    button_owners.add(current)
                owners[block['id']] = current
                if current and block['type'].startswith('heading_') and n.block_date(block, config):
                    dates_per_owner.setdefault(current, set()).add(block['id'])
                walk(block.get('_children', []), current)
        walk(item['blocks'])
        task = item['task']
        requested = [(update['key'].split(':')[-1], 'update') for update in item['updates']]
        requested += [(section['id'], 'objective/deliverable') for section in extract_context(item['blocks'])]
        for ident, kind in requested:
            owner = owners.get(ident)
            if not owner or owner in button_owners or (kind == 'update' and len(dates_per_owner.get(owner, [])) > 1):
                missing.append({'task': n.title(task), 'kind': kind, 'source': source_url(task, ident)})
            else:
                regions[ident] = owner
    return regions, missing


def synced_reference(ident):
    return {'object': 'block', 'type': 'synced_block', 'synced_block': {'synced_from': {'type': 'block_id', 'block_id': ident}}}


def meeting_blocks(items, resolver, config, regions=None):
    regions = regions or {}
    cutoff, today = n.date_window(config)
    count = sum(len(item['updates']) for item in items)
    result = [paragraph(f'{cutoff + dt.timedelta(days=1)} through {today} · {len(items)} tasks · {count} updates · {sum(i["new"] for i in items)} newly created tasks'),
              paragraph('Grouped by project subteam, then project; tasks within each project are ordered by overlapping assignees, reviewers, and participants. Task rows are snapshots; their links open the original tasks.')]
    current_project = current_subteam = None
    for item in items:
        project_key = item.get('meeting_project', tuple(sorted(item['projects'])))
        subteam_key = item.get('meeting_subteams', tuple(sorted(item['subteams'])))
        if subteam_key != current_subteam:
            result.append(heading(' / '.join(subteam_key) or 'No project subteam', 1))
            current_subteam, current_project = subteam_key, None
        if project_key != current_project:
            label = ' / '.join(n.plain(resolver.page(p)) for p in project_key) or 'No project'
            result.append(heading(label, 2))
            current_project = project_key
        task = item['task']
        props = task['properties']
        result.append(heading(n.title(task) + (' · NEW TASK' if item['new'] else ''), 3))
        result.append({'object': 'block', 'type': 'paragraph', 'paragraph': {'rich_text': [{'type': 'mention', 'mention': {'type': 'page', 'page': {'id': task['id']}}}]}})
        participant_ids = sorted({p for r in item['updates'] for p in r['people']})
        participant_names = plain_participants(item['updates'])
        participants = join_rich([resolver.person({'id': p}) for p in participant_ids] + [text(p) for p in participant_names])
        def prop(name):
            return resolver.property(props.get(name, {}))
        result.append(table(['Task', 'Project', 'Subteam', 'Status', 'Priority', 'Assigned to', 'Participants', 'Due'], [[
            text(n.title(task), task['url']), prop(config.get('project_property', 'Project')), prop('Subteam'), prop('Status'),
            prop('Priority Level'), prop('Assigned To'), participants, prop('Due Date')]]))
        details = [[text(name), resolver.property(value)] for name, value in props.items()]
        details += [[text('Created'), text(task.get('created_time', '—'))], [text('Last edited'), text(task.get('last_edited_time', '—'))]]
        result.append(toggle('All task properties', [table(['Property', 'Value'], details)]))
        context = []
        context_owners = set()
        for section in extract_context(item['blocks']):
            owner = regions.get(section['id'])
            if not owner:
                context.append(heading(section['title'], 3))
                context.append({'object': 'block', 'type': 'paragraph', 'paragraph': {'rich_text': text('Source section', source_url(task, section['id']))}})
                context.extend(nb.clone_roots(section['blocks'], task['url']))
            elif owner not in context_owners:
                context.append(synced_reference(owner))
                context_owners.add(owner)
        # API limits each children array to 100 blocks. Split oversized context toggles.
        if item['new']:
            result.extend(context or [paragraph('Objective & deliverables: not specified.')])
        else:
            for part in range(0, max(1, len(context)), 90):
                result.append(toggle('Objective & deliverables' + (' (continued)' if part else ''), context[part:part+90]))
        entries = {e['source_block']: e for e in n.entries(item['blocks'], config)}
        for update in item['updates']:
            ident = update['key'].split(':')[-1]
            if ident in regions:
                result.append(synced_reference(regions[ident]))
            else:
                result.extend(nb.clone_roots(entries[ident]['blocks'], task['url']))
        result.append({'object': 'block', 'type': 'divider', 'divider': {}})
    return result


def ensure_summary_source(api, config):
    database = config.get('updates_database')
    if not database:
        raise ValueError('NOTION_UPDATES_DATABASE_ID must name the existing Updates database')
    source = api.source(database, config.get('updates_data_source_id'))
    api.authorize_updates(database, source, (config.get('tasks_data_source_id'), config.get('projects_data_source_id')))
    properties = api.call('GET', 'data_sources/' + source)['properties']
    required = {'Generated at': {'date': {}}, 'Window': {'date': {}}, 'Tasks': {'number': {}}, 'Updates': {'number': {}},
                'State': {'select': {'options': [{'name': 'Building', 'color': 'yellow'}, {'name': 'Ready', 'color': 'green'}]}}}
    for name, definition in required.items():
        if name in properties and properties[name]['type'] != next(iter(definition)):
            raise ValueError('Updates schema has an incompatible property: ' + name)
    missing = {name: definition for name, definition in required.items() if name not in properties}
    if missing:
        api.call('PATCH', 'data_sources/' + source, {'properties': missing})
    return source


def block_count(block):
    return 1 + sum(block_count(child) for child in block.get(block['type'], {}).get('children', []))


def batches(blocks):
    batch, size, count = [], 0, 0
    for block in blocks:
        encoded = len(json.dumps(block).encode())
        total = block_count(block)
        if encoded > 400000 or total > 900:
            raise ValueError('One summary block exceeds API limits; split the source content')
        if batch and (len(batch) >= 40 or size + encoded > 400000 or count + total > 900):
            yield batch
            batch, size, count = [], 0, 0
        batch.append(block)
        size += encoded
        count += total
    if batch:
        yield batch


def publish(api, source, items, blocks, config):
    cutoff, today = n.date_window(config)
    stamp = dt.datetime.now(ZoneInfo(config.get('timezone', 'America/Chicago'))).strftime('%Y-%m-%d %H:%M:%S')
    # Validate the complete body before creating anything.
    chunks = list(batches(blocks))
    page = api.call('POST', 'pages', {'parent': {'type': 'data_source_id', 'data_source_id': source}, 'properties': {
        'Name': {'title': text(f'Update generated on {stamp} {config.get("timezone", "America/Chicago")}')},
        'Generated at': {'date': {'start': dt.datetime.now(dt.timezone.utc).isoformat()}},
        'Window': {'date': {'start': (cutoff + dt.timedelta(days=1)).isoformat(), 'end': today.isoformat()}},
        'Tasks': {'number': len(items)}, 'Updates': {'number': sum(len(item['updates']) for item in items)},
        'State': {'select': {'name': 'Building'}}, 'Current week': {'checkbox': True}}})
    print('Writing summary: ' + page['url'], file=sys.stderr)
    for index, children in enumerate(chunks):
        nb.materialize_media(api, page['id'], children)
        nb.append_tree(api, page['id'], children)
        print(f'Wrote summary batch {index+1}/{len(chunks)}', file=sys.stderr)
    api.call('PATCH', 'pages/' + page['id'], {'properties': {'State': {'select': {'name': 'Ready'}}}})
    return page['url']


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--env', type=Path, default=n.ROOT / '.env')
    parser.add_argument('--dry-run', action='store_true', help='Save a preview without writing to Notion')
    parser.add_argument('--from-scan', action='store_true', help='Use the last local scan (explicitly; may be stale)')
    args = parser.parse_args()
    n.load_env(args.env)
    token = os.environ.get('NOTION_TOKEN', '').strip()
    if not token:
        raise ValueError('Fill in NOTION_TOKEN in .env')
    config = n.config_from_env()
    with (n.ROOT / '.sync.lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        api = n.API(token)
        cache = n.ROOT / '.cache'
        cache.mkdir(exist_ok=True, mode=0o700)
        with redirect_stdout(sys.stderr):
            if args.from_scan:
                records = json.loads((n.ROOT / 'preview.json').read_text())
            else:
                records = n.collect(api, config['tasks_data_source_id'], config, cache / 'tasks.json')
                (n.ROOT / 'preview.json').write_text(json.dumps(records, ensure_ascii=False, indent=2))
            snapshots = json.loads((cache / 'tasks.json').read_text())
            cutoff, today = n.date_window(config)
            records = [r for r in records if cutoff.isoformat() < r['date'] <= today.isoformat()]
            items = select_tasks(snapshots, records, config)
            resolver = Resolver(api, snapshots, config)
            items = organize(items, resolver)
            regions, missing = live_regions(items, config)
            print(f'{len(missing)} sections will be copied; existing synced sections stay live.', file=sys.stderr)
            blocks = meeting_blocks(items, resolver, config, regions)
            (n.ROOT / 'meeting-preview.json').write_text(json.dumps({'tasks': [n.title(i['task']) for i in items], 'blocks': blocks}, ensure_ascii=False, indent=2))
            print(f'Meeting: {len(items)} tasks, {sum(len(i["updates"]) for i in items)} updates, {sum(i["new"] for i in items)} newly created tasks', file=sys.stderr)
            if args.dry_run:
                print('Preview: ' + str(n.ROOT / 'meeting-preview.json'), file=sys.stderr)
                return
            source = ensure_summary_source(api, config)
            url = publish(api, source, items, blocks, config)
        print(url)
