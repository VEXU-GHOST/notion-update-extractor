import contextlib
import datetime as dt
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
import meeting_summary as m
import notion_feed as n
from test_notion_feed import block, date_block, UID


def snapshot(ident, created='2026-10-06T18:00:00Z', project='project-a'):
    return {'task': {'id': ident, 'url': 'https://www.notion.so/' + ident, 'created_time': created,
            'properties': {'Name': {'type': 'title', 'title': n.rich(ident)},
                'Project': {'type': 'relation', 'relation': [{'id': project}]},
                'Subteam': {'type': 'select', 'select': {'name': 'Software'}},
                'Assigned To': {'type': 'people', 'people': [{'id': UID}]}}},
            'blocks': [block('heading_1', 'Objective', 'objective'), block('paragraph', 'Make a thing'),
                       block('heading_2', 'Deliverables', 'deliverable'), block('paragraph', 'Working prototype'),
                       block('heading_1', 'Progress'), date_block('2026-10-06', 'date'), block('paragraph', 'Built prototype')]}


class SummaryTests(unittest.TestCase):
    def setUp(self):
        self.window = patch.object(n, 'date_window', return_value=(dt.date(2026, 10, 4), dt.date(2026, 10, 11)))
        self.window.start()
        self.addCleanup(self.window.stop)

    def test_created_tasks_without_updates_are_included(self):
        snapshots = [snapshot('new'), snapshot('old', '2026-10-03T18:00:00Z'), snapshot('future', '2026-10-12T18:00:00Z')]
        result = m.select_tasks(snapshots, [], {}, dt.datetime(2026, 10, 11, 20, tzinfo=dt.timezone.utc))
        self.assertEqual([x['task']['id'] for x in result], ['new'])
        self.assertTrue(result[0]['new'])
        self.assertEqual(result[0]['updates'], [])

    def test_creation_uses_configured_local_day(self):
        # 01:00 Monday UTC is still Sunday evening in Chicago, outside the exclusive window.
        item = snapshot('boundary', '2026-10-05T01:00:00Z')
        self.assertEqual(m.select_tasks([item], [], {'timezone': 'America/Chicago'}), [])

    def test_plain_participants_and_assignees_share_identity(self):
        record = {'task_id': 'old', 'date': '2026-10-06', 'key': 'k', 'people': [], 'unmapped': ['Amy Choi', 'Brian / Collin', '2026-10-07']}
        result = m.select_tasks([snapshot('old', '2026-10-01T00:00:00Z')], [record], {'people_map': {'Amy Choi': UID}})[0]
        self.assertEqual(result['people'], {UID, 'name:brian', 'name:collin'})
        self.assertFalse(result['new'])

    def test_order_is_deterministic_preserves_all_tasks_and_groups_projects(self):
        items = m.select_tasks([snapshot('a1'), snapshot('b1', project='project-b'), snapshot('a2'), snapshot('b2', project='project-b')], [], {})
        ordered = m.order_tasks(items)
        self.assertEqual([i['task']['id'] for i in ordered], [i['task']['id'] for i in m.order_tasks(list(reversed(items)))])
        self.assertEqual(len({i['task']['id'] for i in ordered}), 4)
        project_runs = [i['projects'] for i in ordered]
        self.assertEqual(sum(a != b for a, b in zip(project_runs, project_runs[1:])), 1)

    def test_context_precedes_updates_and_links_to_original_sections(self):
        snap = snapshot('task', '2026-10-01T00:00:00Z')
        record = {'task_id': 'task', 'key': 'task:date', 'date': '2026-10-06', 'people': [], 'unmapped': [], 'url': 'https://www.notion.so/task#date', 'text': 'Built prototype'}
        items = m.select_tasks([snap], [record], {})
        api = Mock()
        api.query.return_value = []
        api.call.return_value = {'url': 'https://www.notion.so/project-a', 'properties': {'Name': {'type': 'title', 'title': n.rich('Project A')}}}
        result = m.meeting_blocks(items, m.Resolver(api, [snap], {}), {})
        labels = [n.plain(b.get(b['type'], {}).get('rich_text', [])) for b in result]
        self.assertLess(labels.index('Objective & deliverables'), labels.index('2026-10-06'))
        objective = next(b for b in result if b['type'] == 'toggle' and n.plain(b['toggle']['rich_text']) == 'Objective & deliverables')
        self.assertEqual(objective['toggle']['children'][1]['paragraph']['rich_text'][0]['text']['link']['url'], 'https://www.notion.so/task#objective')
        rows = next(b for b in result if b['type'] == 'table')['table']['children']
        self.assertEqual(rows[1]['table_row']['cells'][0][0]['text']['link']['url'], 'https://www.notion.so/task')

    def test_copy_preserves_rich_text_and_skips_buttons(self):
        import notion_blocks as nb
        original = block('paragraph', 'Bold', 'p')
        original['paragraph']['rich_text'][0]['annotations'] = {'bold': True}
        original['paragraph']['icon'] = None
        copied = nb.clone_roots([original, {'id':'button','type':'unsupported','unsupported':{'block_type':'button'}}], 'https://www.notion.so/task')
        self.assertEqual(len(copied), 1)
        self.assertTrue(copied[0]['paragraph']['rich_text'][0]['annotations']['bold'])
        self.assertNotIn('icon', copied[0]['paragraph'])
        self.assertIn('icon', original['paragraph'])

    def test_new_task_objectives_are_expanded_and_heading_is_plain(self):
        snap = snapshot('task')
        items = m.select_tasks([snap], [], {})
        api = Mock()
        api.query.return_value = []
        api.call.return_value = {'url': 'https://www.notion.so/project-a', 'properties': {}}
        result = m.meeting_blocks(items, m.Resolver(api, [snap], {}), {})
        toggles = [n.plain(b['toggle']['rich_text']) for b in result if b['type'] == 'toggle']
        self.assertNotIn('Objective & deliverables', toggles)
        first_heading = next(b for b in result if b['type'] == 'heading_1')
        self.assertNotIn('link', first_heading['heading_1']['rich_text'][0]['text'])
        self.assertTrue(any(b['type']=='paragraph' and any(r.get('mention',{}).get('type')=='page' for r in b['paragraph']['rich_text']) for b in result))

    def test_groups_by_project_subteam_not_task_subteam(self):
        snaps = [snapshot('a', project='pa'), snapshot('b', project='pb'), snapshot('c', project='pa')]
        items = m.select_tasks(snaps, [], {})
        resolver = Mock()
        resolver.pages = {'pa': {'properties': {'Subteam(s)': {'type':'multi_select','multi_select':[{'name':'Mechanical'}]}}}, 'pb': {'properties': {'Subteam(s)': {'type':'multi_select','multi_select':[{'name':'Hardware'}]}}}}
        resolver.page.side_effect = lambda p: m.text(p)
        ordered = m.organize(items, resolver)
        self.assertEqual([i['meeting_subteams'] for i in ordered], [('Hardware',), ('Mechanical',), ('Mechanical',)])
        self.assertEqual([i['meeting_project'] for i in ordered], [('pb',), ('pa',), ('pa',)])

    def test_stdout_is_only_new_page_url(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root/'.cache').mkdir()
            (root/'.cache/tasks.json').write_text('[]')
            (root/'preview.json').write_text('[]')
            with patch.object(n, 'ROOT', root), patch.dict(m.os.environ, {'NOTION_TOKEN': 'fake'}, clear=True), patch.object(m.sys, 'argv', ['notion_feed.py', '--from-scan']), patch.object(m, 'Resolver'), patch.object(m, 'ensure_summary_source', return_value='summary-source'), patch.object(m, 'publish', return_value='https://www.notion.so/new-summary'), contextlib.redirect_stdout(io.StringIO()) as output, contextlib.redirect_stderr(io.StringIO()):
                m.main()
                self.assertEqual(output.getvalue(), 'https://www.notion.so/new-summary\n')

    def test_summary_upload_is_chunked_and_marked_ready_last(self):
        api = Mock()
        api.call.side_effect = lambda method, path, body=None: {'id': 'new', 'url': 'https://www.notion.so/new', 'results': [{'id': str(i)} for i, _ in enumerate((body or {}).get('children', []))]}
        blocks = [m.paragraph(str(i)) for i in range(85)]
        self.assertEqual(m.publish(api, 'source', [], blocks, {}), 'https://www.notion.so/new')
        append_calls = [c for c in api.call.call_args_list if c.args[1] == 'blocks/new/children']
        self.assertEqual(sum(len(c.args[2]['children']) for c in append_calls), 85)
        self.assertEqual(api.call.call_args_list[-1].args[2]['properties']['State']['select']['name'], 'Ready')

    def test_failed_upload_never_marks_page_ready(self):
        api = Mock()
        api.call.side_effect = [{'id': 'new', 'url': 'https://www.notion.so/new'}, RuntimeError('fail')]
        with self.assertRaises(RuntimeError):
            m.publish(api, 'source', [], [m.paragraph('x')], {})
        self.assertEqual(api.call.call_count, 2)


if __name__ == '__main__':
    unittest.main()
