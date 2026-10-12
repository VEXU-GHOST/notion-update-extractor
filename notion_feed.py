#!/usr/bin/env python3
"""Deterministic Notion progress-entry sync. Python 3.10+, standard library only."""
import argparse
import datetime as dt
import hashlib
import fcntl
import json
import os
from pathlib import Path
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from zoneinfo import ZoneInfo

VERSION = '2025-09-03'
ROOT = Path(__file__).resolve().parent


def load_env(path):
    """Read simple KEY=value settings without executing shell or interpolating secrets."""
    if path.exists():
        for number, line in enumerate(path.read_text().splitlines(), 1):
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            if '=' not in line:
                raise ValueError(f'Invalid .env line {number}')
            key, value = line.split('=', 1)
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
                value = value[1:-1]
            os.environ.setdefault(key.strip(), value)


def config_from_env():
    return {
        'tasks_database': os.environ.get('NOTION_TASKS_DATABASE_ID', ''),
        'tasks_data_source_id': os.environ.get('NOTION_TASKS_DATA_SOURCE_ID'),
        'updates_database': os.environ.get('NOTION_UPDATES_DATABASE_ID'),
        'updates_data_source_id': os.environ.get('NOTION_UPDATES_DATA_SOURCE_ID'),
        'projects_data_source_id': os.environ.get('NOTION_PROJECTS_DATA_SOURCE_ID'),
        'timezone': os.environ.get('TIMEZONE', 'America/Chicago'),
        'undated_year': int(os.environ['UNDATED_YEAR']) if os.environ.get('UNDATED_YEAR') else None,
        'project_property': os.environ.get('PROJECT_PROPERTY', 'Project'),
        'people_map': json.loads((ROOT / 'people_map.json').read_text()) if (ROOT / 'people_map.json').exists() else {},
    }


def save_setting(path, key, value):
    lines = path.read_text().splitlines() if path.exists() else []
    pattern = re.compile(r'^' + re.escape(key) + r'\s*=')
    if any(pattern.match(line) for line in lines):
        lines = [key + '=' + value if pattern.match(line) else line for line in lines]
    else:
        lines.append(key + '=' + value)
    temporary = path.with_suffix('.tmp')
    temporary.touch(mode=0o600, exist_ok=False)
    temporary.write_text('\n'.join(lines) + '\n')
    temporary.replace(path)


def notion_id(value):
    matches = re.findall(r'[0-9a-fA-F]{32}|[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}', value.split('?')[0])
    if not matches:
        raise ValueError('Expected a Notion ID or page/database URL')
    return str(uuid.UUID(matches[-1]))


def rich(text):
    chunks = [text[i:i+1900] for i in range(0, len(text), 1900)]
    if len(chunks) > 100:
        raise ValueError('Update exceeds the supported text size; refusing to truncate')
    return [{'type': 'text', 'text': {'content': x}} for x in chunks]


def plain(items):
    values = []
    for item in items:
        mention = item.get('mention', {})
        fallback = item.get('text', {}).get('content', '')
        if mention.get('type') == 'date':
            fallback = mention['date']['start']
        elif mention.get('type') == 'user':
            fallback = mention['user'].get('name', mention['user']['id'])
        values.append(item.get('plain_text', fallback))
    return ''.join(values)


class AttachmentTooLarge(ValueError):
    pass


class NoAuthRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise RuntimeError('Refusing redirect of an authenticated Notion request')


class API:
    def __init__(self, token):
        self.token = token
        self.auth_opener = urllib.request.build_opener(NoAuthRedirect())
        self.last = 0
        self.write_database = None
        self.write_source = None
        self.writable_ids = set()
        self._pending_import = None

    def authorize_updates(self, database, source, forbidden_sources=()):
        database, source = notion_id(database), notion_id(source)
        if source in {notion_id(s) for s in forbidden_sources if s}:
            raise ValueError('Refusing to authorize a source database for writes')
        metadata = self.call('GET', 'databases/' + database)
        if source not in {notion_id(item['id']) for item in metadata.get('data_sources', [])}:
            raise ValueError('Updates data source does not belong to the configured Updates database')
        self.write_database, self.write_source = database, source
        self.writable_ids.clear()

    def under_updates(self, ident, visited=None):
        ident = notion_id(ident)
        if ident in self.writable_ids:
            return True
        visited = set() if visited is None else visited
        if ident in visited or len(visited) > 50:
            return False
        visited.add(ident)
        block = self.call('GET', 'blocks/' + ident)
        if block.get('type') == 'synced_block':
            return False  # Editing a reference can mutate the read-only original.
        parent = block.get('parent', {})
        if parent.get('type') == 'data_source_id':
            allowed = parent.get('data_source_id') == self.write_source
        elif parent.get('type') == 'database_id':
            allowed = parent.get('database_id') == self.write_database
        elif parent.get('type') in ('page_id', 'block_id'):
            allowed = self.under_updates(parent[parent['type']], visited)
        else:
            allowed = False
        return allowed

    def import_for_summary(self, page, url, filename):
        if page not in self.writable_ids or not self.write_source:
            raise ValueError('Media imports require a verified Updates page')
        # Copy signed source media bytes; the source request carries no Notion token.
        import mimetypes
        for attempt in range(5):
            try:
                with urllib.request.urlopen(url, timeout=60) as response:
                    content = response.read(20 * 1024 * 1024 + 1)
                    content_type = response.headers.get_content_type()
                break
            except OSError:
                if attempt == 4:
                    raise
                time.sleep(2 ** attempt)
        if len(content) > 20 * 1024 * 1024:
            raise AttachmentTooLarge('Attachment exceeds the 20 MB single-part copy limit')
        body = {'mode': 'single_part', 'filename': filename,
                'content_type': mimetypes.guess_type(filename)[0] or content_type}
        self._pending_import = body
        try:
            upload = self.call('POST', 'file_uploads', body)
        finally:
            self._pending_import = None
        boundary = 'notioncopy' + uuid.uuid4().hex
        safe_name = filename.replace('"', '_').replace('\r', '_').replace('\n', '_')
        prefix = (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{safe_name}"\r\n'
                  f'Content-Type: {body["content_type"]}\r\n\r\n').encode()
        data = prefix + content + f'\r\n--{boundary}--\r\n'.encode()
        # Fixed endpoint and freshly created upload ID; never use a source upload URL.
        request = urllib.request.Request('https://api.notion.com/v1/file_uploads/' + notion_id(upload['id']) + '/send',
            data=data, method='POST', headers={'Authorization': 'Bearer ' + self.token,
            'Notion-Version': VERSION, 'Content-Type': 'multipart/form-data; boundary=' + boundary})
        with self.auth_opener.open(request, timeout=90) as response:
            return json.load(response)

    def check_write(self, method, path, body):
        if method == 'GET':
            return
        segments = path.split('?')[0].split('/')
        if method == 'POST' and len(segments) == 3 and segments[0] == 'data_sources' and segments[2] == 'query':
            return  # This POST is a read-only query.
        if any('relation' in value for value in (body or {}).get('properties', {}).values() if isinstance(value, dict)):
            raise ValueError('Relation writes are disabled to protect reciprocal source properties')
        if not self.write_source:
            raise ValueError('Notion writes are disabled until the Updates destination is verified')
        if method == 'POST' and path == 'file_uploads' and body is self._pending_import and body is not None:
            return  # New attachment for a verified Updates page, never a source mutation.
        if method == 'POST' and path == 'pages':
            parent = (body or {}).get('parent', {})
            if parent.get('type') == 'data_source_id' and parent.get('data_source_id') == self.write_source:
                return
        elif method == 'PATCH' and segments == ['data_sources', self.write_source]:
            if set(body or {}) <= {'properties'}:
                return
        elif segments[0] in ('pages', 'blocks') and len(segments) in (2, 3):
            allowed_operation = (len(segments) == 2 and method in ('PATCH', 'DELETE')) or (segments[0] == 'blocks' and len(segments) == 3 and segments[2] == 'children' and method == 'PATCH')
            if allowed_operation and 'parent' not in (body or {}) and self.under_updates(segments[1]):
                return
        raise ValueError(f'Blocked Notion write outside Updates: {method} {path}')

    def call(self, method, path, body=None):
        self.check_write(method, path, body)
        for attempt in range(6):
            time.sleep(max(0, .36 - (time.monotonic() - self.last)))
            self.last = time.monotonic()
            req = urllib.request.Request('https://api.notion.com/v1/' + path,
                data=None if body is None else json.dumps(body).encode(), method=method,
                headers={'Authorization': 'Bearer ' + self.token,
                         'Notion-Version': VERSION, 'Content-Type': 'application/json'})
            try:
                with self.auth_opener.open(req, timeout=60) as response:
                    result = json.load(response)
                    if method == 'POST' and path == 'pages':
                        self.writable_ids.add(result['id'])
                    return result
            except urllib.error.HTTPError as error:
                if error.code == 429 and attempt < 5:
                    time.sleep(float(error.headers.get('Retry-After', '2')))
                    continue
                # Retrying a timed-out create can duplicate pages. Stop and reconcile next run.
                if method == 'GET' and error.code >= 500 and attempt < 5:
                    time.sleep(2 ** attempt)
                    continue
                detail = json.loads(error.read()).get('message', 'check access, IDs, and schema')
                raise RuntimeError(f'Notion HTTP {error.code} at {path}: {detail}') from None
            except (OSError, __import__('http.client', fromlist=['HTTPException']).HTTPException):
                is_read = method == 'GET' or (method == 'POST' and path.startswith('data_sources/') and path.endswith('/query'))
                if is_read and attempt < 5:
                    time.sleep(2 ** attempt)
                    continue
                raise
        raise RuntimeError('Retry limit reached')

    def query(self, source):
        cursor = None
        while True:
            body = {'page_size': 100}
            if cursor:
                body['start_cursor'] = cursor
            result = self.call('POST', f'data_sources/{source}/query', body)
            if source == self.write_source:
                self.writable_ids.update(page['id'] for page in result['results'])
            yield from result['results']
            if not result.get('has_more'):
                break
            cursor = result['next_cursor']

    def children(self, block):
        cursor = None
        while True:
            path = f'blocks/{block}/children?page_size=100'
            if cursor:
                path += '&start_cursor=' + urllib.parse.quote(cursor)
            result = self.call('GET', path)
            for item in result['results']:
                # A child page/database is a separate document, not part of this entry.
                if item.get('has_children') and item['type'] not in ('child_page', 'child_database'):
                    item['_children'] = list(self.children(item['id']))
                yield item
            if not result.get('has_more'):
                break
            cursor = result['next_cursor']

    def users(self):
        cursor = None
        while True:
            path = 'users?page_size=100'
            if cursor:
                path += '&start_cursor=' + urllib.parse.quote(cursor)
            result = self.call('GET', path)
            yield from result['results']
            if not result.get('has_more'):
                break
            cursor = result['next_cursor']

    def source(self, database, explicit=None):
        if explicit:
            return notion_id(explicit)
        result = self.call('GET', 'databases/' + notion_id(database))
        sources = result.get('data_sources', [])
        if len(sources) != 1:
            raise ValueError('Database has zero or multiple data sources; configure its data_source_id')
        return sources[0]['id']


def block_rich(block):
    content = block.get(block['type'], {})
    if block['type'] == 'table_row':
        return [item for cell in content.get('cells', []) for item in cell]
    return content.get('rich_text', [])


def block_text(block):
    kind = block['type']
    text = plain(block_rich(block))
    if text:
        return text
    if kind == 'child_page':
        return block[kind].get('title', 'Child page') + ' — https://www.notion.so/' + block['id'].replace('-', '')
    if kind == 'paragraph':
        return ''
    return '[' + kind + ']'


def block_date(block, config):
    mentions = [x['mention']['date']['start'] for x in block_rich(block)
                if x.get('mention', {}).get('type') == 'date']
    if len(mentions) == 1:
        return dt.date.fromisoformat(mentions[0][:10])
    return heading_date(block_text(block), config.get('undated_year'))


def heading_date(text, year=None):
    # Require the whole heading to be a date, optionally preceded by Update/Progress.
    value = re.sub(r'^(?:update|progress)(?:\s*[:\-]\s*|\s+)', '', text.strip(), flags=re.I)
    formats = ['%Y-%m-%d', '%B %d, %Y', '%b %d, %Y', '%B %d %Y', '%b %d %Y', '%m/%d/%Y']
    for fmt in formats:
        try:
            return dt.datetime.strptime(value, fmt).date()
        except ValueError:
            pass
    if year:
        for fmt in ['%B %d %Y', '%b %d %Y', '%m/%d %Y']:
            try:
                return dt.datetime.strptime(f'{value} {year}', fmt).date()
            except ValueError:
                pass
    return None


def flatten(blocks):
    for block in blocks:
        yield block
        yield from flatten(block.get('_children', []))


def entries(blocks, config):
    current = None
    boundary = None
    for block in flatten(blocks):
        kind = block['type']
        if kind in ('synced_block', 'column_list', 'column'):
            continue
        heading = kind.startswith('heading_')
        level = int(kind[-1]) if heading else None
        date = block_date(block, config) if heading else None
        if date:
            if current:
                yield current
            current = {'source_block': block['id'], 'date': date.isoformat(), 'blocks': [block]}
            boundary = level or 3
        elif heading and current and level <= boundary:
            yield current
            current = None
        elif current:
            current['blocks'].append(block)
    if current:
        yield current


def participant_ids(blocks, config):
    ids, unresolved = set(), set()
    labels = re.compile(r'^(?:participants|people|members|worked by)\s*:', re.I)
    mapping = {key.casefold(): value for key, value in config.get('people_map', {}).items()}
    active_level = None
    for block in blocks:
        text = block_text(block).strip()
        kind = block['type']
        if kind.startswith('heading_'):
            level = int(kind[-1])
            if active_level and level <= active_level:
                active_level = None
            if text.rstrip(':').casefold() in ('participants', 'people', 'members'):
                active_level = level
                continue
        inline = labels.match(text)
        if not active_level and not inline:
            continue
        parts = block_rich(block)
        for item in parts:
            mention = item.get('mention', {})
            if mention.get('type') == 'user':
                ids.add(mention['user']['id'])
        names = ''.join(';' if item.get('mention') else plain([item]) for item in parts)
        names = labels.sub('', names)
        for name in re.split(r'[,;/\n]|\s+&\s+|\s+and\s+', names):
            name = name.strip().lstrip('@').strip()
            if name:
                if name.casefold() in mapping:
                    ids.add(notion_id(mapping[name.casefold()]))
                else:
                    unresolved.add(name)
    return sorted(ids), sorted(unresolved)


def meaningful(blocks):
    # Date buttons often create empty templates. Do not publish those as updates.
    for block in blocks[1:]:
        if block['type'].startswith('heading_') and block_text(block).strip().casefold() in ('progress', 'participants', 'future steps'):
            continue
        if block['type'] == 'divider':
            continue
        if block_text(block).strip():
            return True
    return False


def render_block(block):
    kind = block['type']
    text = block_text(block)
    if not block_rich(block) and kind not in ('paragraph', 'child_page'):
        text += ' — https://www.notion.so/' + block['id'].replace('-', '')
    if kind.startswith('heading_'):
        return '#' * int(kind[-1]) + ' ' + text
    if kind == 'bulleted_list_item':
        return '- ' + text
    if kind == 'numbered_list_item':
        return '1. ' + text
    if kind == 'to_do':
        return ('[x] ' if block[kind].get('checked') else '[ ] ') + text
    return text


def title(page):
    return next((plain(p['title']) for p in page['properties'].values() if p['type'] == 'title'), 'Untitled')


def schema(task_source, project_source=None):
    result = {'Name': {'title': {}}, 'Date': {'date': {}}, 'Current week': {'checkbox': {}}, 'Text': {'rich_text': {}},
            'Participants': {'people': {}}, 'Unmapped participants': {'rich_text': {}},
            'Task': {'relation': {'data_source_id': task_source, 'single_property': {}}},
            'Projects': {'rich_text': {}}, 'Source': {'url': {}},
            'Source key': {'rich_text': {}}, 'Content hash': {'rich_text': {}}}
    if project_source:
        result['Project'] = {'relation': {'data_source_id': project_source, 'single_property': {}}}
    return result


def date_window(config, today=None):
    today = today or dt.datetime.now(ZoneInfo(config.get('timezone', 'America/Chicago'))).date()
    # On Sunday, 'last Sunday' means seven days ago, not this morning.
    cutoff = today - dt.timedelta(days=(today.weekday() + 1) % 7 or 7)
    return cutoff, today


def collect(api, source, config, snapshot_path=None):
    cutoff, today = date_window(config)
    print(f'Window: after {cutoff.isoformat()} through {today.isoformat()}')
    records = []
    # Resolve only unique, exact display-name matches; ambiguous names stay unmapped.
    names = {}
    try:
        users = list(api.users())
    except RuntimeError as error:
        if 'HTTP 403' not in str(error):
            raise
        print('User-directory permission unavailable; using people_map.json and direct mentions.', file=sys.stderr)
        users = []
    for user in users:
        if user.get('type') == 'person' and user.get('name'):
            names.setdefault(user['name'].casefold(), set()).add(user['id'])
    config = dict(config)
    config['people_map'] = {**{name: next(iter(ids)) for name, ids in names.items() if len(ids) == 1}, **config.get('people_map', {})}
    project_cache = {}
    snapshots = []
    for task in api.query(source):
        blocks = list(api.children(task['id']))
        snapshots.append({'task': task, 'blocks': blocks})
        if snapshot_path:
            Path(snapshot_path).write_text(json.dumps(snapshots, ensure_ascii=False))
        found = list(entries(blocks, config))
        if not found:
            print(f'No recognized dated sections: {title(task)}', file=sys.stderr)
        projects = []
        prop = task['properties'].get(config.get('project_property', 'Project'), {})
        if prop.get('has_more'):
            raise ValueError('Task has a paginated Project relation; extend relation retrieval before syncing')
        for relation in prop.get('relation', []):
            pid = relation['id']
            if pid not in project_cache:
                project_cache[pid] = api.call('GET', 'pages/' + pid)
            project = project_cache[pid]
            projects.append(f"{title(project)} — {project['url']}")
        for entry in found:
            if not cutoff < dt.date.fromisoformat(entry['date']) <= today or not meaningful(entry['blocks']):
                continue
            people, unknown = participant_ids(entry['blocks'], config)
            record = {'key': task['id'] + ':' + entry['source_block'],
                      'task_id': task['id'], 'name': title(task) + ' — ' + entry['date'],
                      'date': entry['date'], 'text': '\n'.join(render_block(b) for b in entry['blocks']),
                      'project_ids': [r['id'] for r in prop.get('relation', [])],
                      'people': people, 'unmapped': unknown, 'projects': '\n'.join(projects),
                      'url': task['url'].split('#')[0] + '#' + entry['source_block'].replace('-', '')}
            record['hash'] = hashlib.sha256(json.dumps(record, sort_keys=True).encode()).hexdigest()
            records.append(record)
    return records


def properties(record, project_source=None):
    result = {'Name': {'title': rich(record['name'][:1900])}, 'Date': {'date': {'start': record['date']}}, 'Current week': {'checkbox': True},
            'Text': {'rich_text': rich(record['text'])},
            'Participants': {'people': [{'object': 'user', 'id': p} for p in record['people']]},
            'Unmapped participants': {'rich_text': rich('; '.join(record['unmapped']))},
            'Task': {'relation': [{'id': record['task_id']}]},
            'Projects': {'rich_text': rich(record['projects'])}, 'Source': {'url': record['url']},
            'Source key': {'rich_text': rich(record['key'])},
            'Content hash': {'rich_text': rich(record['hash'])}}
    if project_source:
        result['Project'] = {'relation': [{'id': pid} for pid in record['project_ids']]}
    return result


def sync_body(api, page_id, text):
    # Only generated pages are managed. A failed run is repaired on the next run.
    chunks = [text[i:i+1800] for i in range(0, len(text), 1800)] or ['']
    old = list(api.children(page_id))
    for index, chunk in enumerate(chunks):
        content = {'rich_text': rich(chunk)}
        if index < len(old):
            block = old[index]
            if block['type'] != 'paragraph':
                raise ValueError('Generated page body contains a manually added non-paragraph block')
            if plain(block_rich(block)) != chunk:
                api.call('PATCH', 'blocks/' + block['id'], {'paragraph': content})
    for start in range(len(old), len(chunks), 50):
        api.call('PATCH', f'blocks/{page_id}/children', {'children': [
            {'object': 'block', 'type': 'paragraph', 'paragraph': {'rich_text': rich(chunk)}}
            for chunk in chunks[start:start+50]]})
    for extra in old[len(chunks):]:
        api.call('DELETE', 'blocks/' + extra['id'])


def sync(api, destination, records, task_source, project_source=None, config=None):
    actual = api.call('GET', 'data_sources/' + destination)['properties']
    for name, definition in schema(task_source, project_source).items():
        if actual.get(name, {}).get('type') != next(iter(definition)):
            raise ValueError(f'Updates property missing or wrong type: {name}')
    existing = {}
    for page in api.query(destination):
        key = plain(page['properties']['Source key']['rich_text'])
        if key in existing:
            raise ValueError('Duplicate Source key in destination; resolve duplicates before syncing')
        if key:
            existing[key] = page
    cutoff, today = date_window(config or {})
    for old in existing.values():
        date = old['properties']['Date'].get('date')
        current = bool(date and cutoff.isoformat() < date['start'][:10] <= today.isoformat())
        if old['properties'].get('Current week', {}).get('checkbox') != current:
            api.call('PATCH', 'pages/' + old['id'], {'properties': {'Current week': {'checkbox': current}}})
    # Validate all payload lengths before any writes.
    prepared = [(r, properties(r, project_source)) for r in records]
    for record, props in prepared:
        old = existing.get(record['key'])
        if old and plain(old['properties']['Content hash']['rich_text']) == record['hash']:
            continue
        if not old:
            # Leave hash unset until the entire body has been written successfully.
            pending = {**props, 'Content hash': {'rich_text': []}}
            old = api.call('POST', 'pages', {'parent': {'type': 'data_source_id', 'data_source_id': destination},
                        'properties': pending})
            existing[record['key']] = old
        sync_body(api, old['id'], record['text'])
        api.call('PATCH', 'pages/' + old['id'], {'properties': props})
        print('Synced:', record['name'])


def update_entries_main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--env', type=Path, default=ROOT / '.env')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--dry-run', action='store_true', help='Preview only; do not write to Notion')
    mode.add_argument('--apply', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--preview', type=Path, default=ROOT / 'preview.json')
    args = parser.parse_args()
    load_env(args.env)
    config = config_from_env()
    token = os.environ.get('NOTION_TOKEN', '').strip()
    if not token or token == 'PASTE_TOKEN_HERE':
        raise ValueError('Fill in NOTION_TOKEN in .env first')
    api = API(token)
    source = api.source(config['tasks_database'], config.get('tasks_data_source_id'))
    # One local process at a time, including initialization. Use just one scheduler/host.
    with (ROOT / '.sync.lock').open('w') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('Another sync is already running') from None
        records = collect(api, source, config)
        args.preview.write_text(json.dumps(records, ensure_ascii=False, indent=2) + '\n')
        print(f'Extracted {len(records)} entries; preview saved to {args.preview}')
        unknown = sorted({name for record in records for name in record['unmapped']})
        if unknown:
            print('Unmapped participants (preserved as text):', ', '.join(unknown))
        if not args.dry_run:
            if not config.get('updates_database') and not config.get('updates_data_source_id'):
                raise ValueError('Create Updates with --init first, or configure an existing destination')
            destination = api.source(config.get('updates_database', ''), config.get('updates_data_source_id'))
            if destination == source:
                raise ValueError('Source and destination must be different data sources')
            api.authorize_updates(config['updates_database'], destination, (source, config.get('projects_data_source_id')))
            sync(api, destination, records, source, config.get('projects_data_source_id'), config)
            print(f'Sync complete: {len(records)} current-window entries')
            if config.get('updates_database'):
                print('Updates database: https://www.notion.so/' + notion_id(config['updates_database']).replace('-', ''))


def main():
    from meeting_summary import main as summary_main
    summary_main()


if __name__ == '__main__':
    try:
        main()
    except (ValueError, RuntimeError, OSError, KeyError) as error:
        print('Error:', str(error), file=sys.stderr)
        sys.exit(1)
