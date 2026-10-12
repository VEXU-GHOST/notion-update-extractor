import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import notion_feed as n

UID = '0e88f5cc-3e77-4b59-966d-3d2d7d950122'


def block(kind, text='', ident='b', children=None, parts=None):
    result = {'id': ident, 'type': kind, kind: {'rich_text': n.rich(text) if parts is None else parts}}
    if children:
        result['_children'] = children
    return result


def date_block(date, ident):
    return block('heading_2', ident=ident, parts=[{'type': 'mention', 'mention': {
        'type': 'date', 'date': {'start': date}}, 'plain_text': date}])


def sample():
    return [date_block('2026-10-11', 'empty'), block('heading_3', 'Progress'), block('paragraph'),
            block('heading_3', 'Participants'), block('paragraph'), block('heading_3', 'Future Steps'),
            date_block('2026-10-06', 'update'), block('heading_3', 'Progress'),
            block('paragraph', 'Designed states', children=[block('bulleted_list_item', 'time_remaining')]),
            block('heading_3', 'Participants'), block('bulleted_list_item', 'Amy Choi'),
            block('bulleted_list_item', 'Unmapped Person'), block('heading_3', 'Future Steps'),
            block('paragraph', 'Train baseline'), date_block('2026-10-04', 'old'),
            block('paragraph', 'Old entry'), block('heading_1', 'Testing'), block('paragraph', 'Outside entry')]


class FakeAPI:
    def __init__(self):
        self.pages = []
        self.bodies = {}
        self.creates = 0
        self.fail_body = False
        self.tasks = [{'id': 'task', 'url': 'https://www.notion.so/task', 'properties': {
            'Name': {'type': 'title', 'title': n.rich('MCTS')},
            'Project': {'relation': [{'id': 'p1'}, {'id': 'p2'}, {'id': 'p3'}]}}}]

    def users(self):
        return iter([{'id': UID, 'name': 'Amy Choi', 'type': 'person'}])

    def query(self, source):
        return iter(self.tasks if source == 'tasks' else self.pages)

    def children(self, parent):
        return iter(sample() if parent == 'task' else copy.deepcopy(self.bodies.get(parent, [])))

    def call(self, method, path, body=None):
        if path == 'data_sources/dest':
            return {'properties': {name: {'type': next(iter(value))} for name, value in n.schema('tasks', 'projects').items()}}
        if method == 'GET' and path.startswith('pages/p'):
            return {'url': 'https://www.notion.so/' + path[6:], 'properties': {'Name': {'type': 'title', 'title': n.rich(path[6:])}}}
        if method == 'POST' and path == 'pages':
            self.creates += 1
            page = {'id': 'entry', 'properties': copy.deepcopy(body['properties'])}
            self.pages.append(page)
            return page
        if method == 'PATCH' and path.endswith('/children'):
            if self.fail_body:
                self.fail_body = False
                raise RuntimeError('Simulated interrupted body write')
            parent = path.split('/')[1]
            for child in body['children']:
                self.bodies.setdefault(parent, []).append({**copy.deepcopy(child), 'id': 'block' + str(len(self.bodies.get(parent, [])))})
            return {}
        if method == 'PATCH' and path.startswith('pages/'):
            self.pages[0]['properties'].update(copy.deepcopy(body['properties']))
            return self.pages[0]
        if method == 'PATCH' and path.startswith('blocks/'):
            for b in self.bodies['entry']:
                if b['id'] == path.split('/')[1]:
                    b.update(copy.deepcopy(body))
            return {}
        raise AssertionError((method, path))


class WindowTests(unittest.TestCase):
    def test_rolling_week_on_sunday_monday_and_midweek(self):
        for today, expected in [('2026-10-11', '2026-10-04'), ('2026-10-12', '2026-10-11'), ('2026-10-14', '2026-10-11'), ('2027-01-01', '2026-12-27')]:
            cutoff, end = n.date_window({}, n.dt.date.fromisoformat(today))
            self.assertEqual(str(cutoff), expected)
            self.assertEqual(str(end), today)


class Tests(unittest.TestCase):
    def setUp(self):
        self.window = patch.object(n, 'date_window', return_value=(n.dt.date(2026, 10, 4), n.dt.date(2026, 10, 11)))
        self.window.start()
        self.addCleanup(self.window.stop)

    def test_date_mentions_section_boundaries_and_empty_templates(self):
        entries = list(n.entries(sample(), {}))
        self.assertEqual([e['date'] for e in entries], ['2026-10-11', '2026-10-06', '2026-10-04'])
        self.assertFalse(n.meaningful(entries[0]['blocks']))
        self.assertTrue(n.meaningful(entries[1]['blocks']))
        self.assertNotIn('Outside entry', [n.block_text(b) for b in entries[-1]['blocks']])
        self.assertIn('time_remaining', [n.block_text(b) for b in entries[1]['blocks']])

    def test_people_section_stops_before_future_steps(self):
        blocks = list(n.entries(sample(), {}))[1]['blocks']
        people, unknown = n.participant_ids(blocks, {'people_map': {'Amy Choi': UID}})
        self.assertEqual(people, [UID])
        self.assertEqual(unknown, ['Unmapped Person'])

    def test_mentions_are_people_only_inside_participants(self):
        mention = {'type': 'mention', 'mention': {'type': 'user', 'user': {'id': UID}}, 'plain_text': 'Amy Choi'}
        self.assertEqual(n.participant_ids([block('paragraph', parts=[mention])], {}), ([], []))
        self.assertEqual(n.participant_ids([block('heading_3', 'Participants'), block('paragraph', parts=[mention])], {}), ([UID], []))

    def test_cutoff_and_multiple_projects_do_not_duplicate(self):
        records = n.collect(FakeAPI(), 'tasks', {})
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]['date'], '2026-10-06')
        self.assertEqual(records[0]['people'], [UID])
        self.assertEqual(records[0]['project_ids'], ['p1', 'p2', 'p3'])

    def test_rerun_does_not_duplicate_and_edited_entry_updates(self):
        api = FakeAPI()
        records = n.collect(api, 'tasks', {})
        n.sync(api, 'dest', records, 'tasks', 'projects')
        n.sync(api, 'dest', records, 'tasks', 'projects')
        self.assertEqual(api.creates, 1)
        records[0].update(text='Changed content', hash='changed')
        n.sync(api, 'dest', records, 'tasks', 'projects')
        self.assertEqual(api.creates, 1)
        self.assertEqual(n.plain(api.bodies['entry'][0]['paragraph']['rich_text']), 'Changed content')

    def test_interrupted_create_repairs_same_page_next_run(self):
        api = FakeAPI()
        records = n.collect(api, 'tasks', {})
        api.fail_body = True
        with self.assertRaises(RuntimeError):
            n.sync(api, 'dest', records, 'tasks', 'projects')
        self.assertEqual(n.plain(api.pages[0]['properties']['Content hash']['rich_text']), '')
        n.sync(api, 'dest', records, 'tasks', 'projects')
        self.assertEqual(api.creates, 1)
        self.assertEqual(n.plain(api.pages[0]['properties']['Content hash']['rich_text']), records[0]['hash'])

    def test_api_query_paginates(self):
        api = n.API('fake')
        with patch.object(api, 'call', side_effect=[{'results': [1], 'has_more': True, 'next_cursor': 'next'}, {'results': [2], 'has_more': False}]) as call:
            self.assertEqual(list(api.query('source')), [1, 2])
            self.assertEqual(call.call_args_list[1].args[2]['start_cursor'], 'next')

    def test_date_formats_do_not_guess_missing_year(self):
        self.assertIsNone(n.heading_date('October 6'))
        self.assertEqual(str(n.heading_date('October 6', 2026)), '2026-10-06')
        self.assertIsNone(n.heading_date('2026-99-99'))

    def test_old_entries_leave_current_view_without_being_deleted(self):
        api = FakeAPI()
        records = n.collect(api, 'tasks', {})
        n.sync(api, 'dest', records, 'tasks', 'projects')
        with patch.object(n, 'date_window', return_value=(n.dt.date(2026, 10, 11), n.dt.date(2026, 10, 12))):
            n.sync(api, 'dest', [], 'tasks', 'projects')
        self.assertEqual(len(api.pages), 1)
        self.assertFalse(api.pages[0]['properties']['Current week']['checkbox'])

    def test_future_dated_entry_is_excluded(self):
        api = FakeAPI()
        original = api.children
        api.children = lambda parent: iter([date_block('2026-10-12', 'future'), block('paragraph', 'Not yet')]) if parent == 'task' else original(parent)
        self.assertEqual(n.collect(api, 'tasks', {}), [])

    def test_directory_permission_falls_back_to_verified_mapping(self):
        api = FakeAPI()
        api.users = lambda: (_ for _ in ()).throw(RuntimeError('Notion HTTP 403 at users'))
        records = n.collect(api, 'tasks', {'people_map': {'Amy Choi': UID}})
        self.assertEqual(records[0]['people'], [UID])

    def test_default_run_publishes_and_dry_run_does_not(self):
        for args, expected in [([], True), (['--dry-run'], False)]:
            with tempfile.TemporaryDirectory() as temp, patch.object(n, 'ROOT', Path(temp)), patch.dict(n.os.environ, {'NOTION_TOKEN': 'fake', 'NOTION_TASKS_DATABASE_ID': 'tasks', 'NOTION_UPDATES_DATABASE_ID': '11111111-1111-4111-8111-111111111111'}, clear=True), patch.object(n.sys, 'argv', ['notion_feed.py'] + args), patch.object(n, 'API') as api, patch.object(n, 'collect', return_value=[]), patch.object(n, 'sync') as sync:
                api.return_value.source.side_effect = ['tasks', 'dest']
                n.update_entries_main()
                self.assertEqual(sync.called, expected)

    def test_env_is_not_shell_and_preserves_existing_environment(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(n.os.environ, {'EXISTING': 'keep'}, clear=True):
            path = Path(temp) / '.env'
            path.write_text('EXISTING=replace\nVALUE="$(echo never-run)"\n')
            n.load_env(path)
            self.assertEqual(n.os.environ['EXISTING'], 'keep')
            self.assertEqual(n.os.environ['VALUE'], '$(echo never-run)')
            n.save_setting(path, 'NEW', 'value')
            self.assertIn('NEW=value', path.read_text())
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)


if __name__ == '__main__':
    unittest.main()
