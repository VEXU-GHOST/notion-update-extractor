"""Copy native content or reference existing synced blocks; omit buttons."""
import copy


def append_tree(api, parent, blocks):
    """Append native trees without exceeding Notion's per-request nesting limit."""
    for start in range(0, len(blocks), 30):
        group = blocks[start:start+30]
        prepared = []
        for block in group:
            value = copy.deepcopy(block)
            kind = value['type']
            if kind == 'numbered_list_item':
                value[kind].pop('list_format', None)
                value[kind].pop('list_start_index', None)
            children = value[kind].pop('children', [])
            if kind == 'table':
                value[kind]['children'] = children[:100]
            elif kind == 'column_list':
                value[kind]['children'] = [
                    {'object': 'block', 'type': 'column', 'column': {'children': [
                        {'object': 'block', 'type': 'paragraph', 'paragraph': {'rich_text': []}}]}}
                    for _ in children]
            prepared.append(value)
        response = api.call('PATCH', f'blocks/{parent}/children', {'children': prepared})
        for original, created in zip(group, response['results']):
            kind = original['type']
            children = original[kind].get('children', [])
            if not children:
                continue
            if kind == 'table':
                if len(children) > 100:
                    append_tree(api, created['id'], children[100:])
            elif kind == 'column_list':
                columns = list(api.children(created['id']))
                for source_column, column in zip(children, columns):
                    append_tree(api, column['id'], source_column['column'].get('children', []))
                    for placeholder in column.get('_children', []):
                        api.call('DELETE', 'blocks/' + placeholder['id'])
            else:
                append_tree(api, created['id'], children)


def clean_rich(items):
    result = []
    for item in items:
        kind = item['type']
        value = copy.deepcopy(item[kind])
        if kind == 'mention' and value.get('type') in ('link_mention', 'link_preview'):
            link = value[value['type']]
            kind, value = 'text', {'content': item.get('plain_text') or link.get('title') or link.get('href') or link.get('url', 'Link'), 'link': {'url': link.get('href') or link.get('url')}}
        if kind == 'text' and value.get('link'):
            url = value['link'].get('url') or ''
            if url.startswith('/p/'):
                value['link']['url'] = 'https://www.notion.so/' + url[3:]
            elif not url.startswith(('https://', 'http://', 'mailto:', 'tel:')):
                value.pop('link', None)  # Keep label; local file paths are not web links.
        if kind == 'mention' and value.get('type') == 'user':
            value['user'] = {'object': 'user', 'id': value['user']['id']}
        values = [value]
        if kind == 'text' and len(value.get('content', '')) > 2000:
            values = [{**value, 'content': value['content'][i:i+2000]} for i in range(0, len(value['content']), 2000)]
        result.extend({'type': kind, kind: part, **({'annotations': item['annotations']} if 'annotations' in item else {})} for part in values)
    if len(result) > 100:
        raise ValueError('Source rich text exceeds Notion’s 100-fragment limit after splitting')
    return result


def clone_roots(blocks, page_url):
    # Entries contain flattened descendants; retain each original subtree only once.
    descendants = {child['id'] for block in blocks for child in walk(block.get('_children', []))}
    return [value for b in blocks if b['id'] not in descendants for value in [clone(b, page_url)] if value]


def walk(blocks):
    for b in blocks:
        yield b
        yield from walk(b.get('_children', []))


def source_link(label, url):
    return {'object': 'block', 'type': 'paragraph', 'paragraph': {'rich_text': clean_rich([
        {'type': 'text', 'text': {'content': label, 'link': {'url': url}}}])}}


def is_button(block):
    return block['type'] == 'button' or (block['type'] == 'unsupported' and block.get('unsupported', {}).get('block_type') == 'button')


def clone(block, page_url):
    kind = block['type']
    if is_button(block):
        return None
    allowed = {'audio', 'video', 'pdf', 'file', 'bookmark', 'embed', 'equation', 'callout', 'table_of_contents', 'breadcrumb', 'paragraph', 'heading_1', 'heading_2', 'heading_3', 'bulleted_list_item', 'numbered_list_item', 'to_do', 'table', 'table_row', 'image', 'quote', 'code', 'toggle', 'column', 'column_list', 'synced_block', 'divider'}
    payload = {k: copy.deepcopy(v) for k, v in block.get(kind, {}).items() if v is not None}
    if kind == 'numbered_list_item':
        payload.pop('list_format', None)
        payload.pop('list_start_index', None)
    url = page_url.split('#')[0] + '#' + block['id'].replace('-', '')
    if kind == 'synced_block' and any(is_button(child) for child in walk(block.get('_children', []))):
        children = [value for child in block.get('_children', []) for value in [clone(child, page_url)] if value]
        return {'object': 'block', 'type': 'paragraph', 'paragraph': {'rich_text': [], 'children': children}}
    if kind == 'synced_block':
        original = payload.get('synced_from')
        payload = {'synced_from': {'type': 'block_id', 'block_id': original['block_id'] if original else block['id']}}
    elif kind not in allowed:
        if kind == 'link_to_page':
            target = payload.get(payload.get('type'), block['id'])
            url = 'https://www.notion.so/' + target.replace('-', '')
        elif kind in ('child_page', 'child_database'):
            url = 'https://www.notion.so/' + block['id'].replace('-', '')
        label = payload.get('title') or payload.get('url') or ('Open original ' + payload.get('block_type', kind).replace('_', ' '))
        result = source_link(label, payload.get('url') or url)
        children = [value for child in block.get('_children', []) for value in [clone(child, page_url)] if value]
        if children:
            result['paragraph']['children'] = children
        return result
    else:
        for name in ('rich_text', 'caption'):
            if name in payload:
                payload[name] = clean_rich(payload[name])
        if kind == 'table_row':
            payload['cells'] = [clean_rich(cell) for cell in payload['cells']]
        if kind in ('image', 'video', 'audio', 'pdf', 'file') and payload.get('type') == 'file':
            # Resolved from a fresh source GET during publication, never persist expiring URLs.
            payload = {k:v for k,v in payload.items() if k not in ('file', 'type')}
            payload['_source_media_block'] = block['id']
            payload['_source_media_url'] = url
        if block.get('_children'):
            payload['children'] = [value for b in block['_children'] for value in [clone(b, page_url)] if value]
    return {'object': 'block', 'type': kind, kind: payload}


def materialize_media(api, page, blocks):
    import time
    import urllib.parse
    for block in blocks:
        payload = block[block['type']]
        ident = payload.pop('_source_media_block', None)
        source_url = payload.pop('_source_media_url', None)
        if ident:
            original = api.call('GET', 'blocks/' + ident)[block['type']]
            url = original['file']['url']
            filename = urllib.parse.unquote(urllib.parse.urlparse(url).path.rsplit('/', 1)[-1])
            from notion_feed import AttachmentTooLarge
            try:
                upload = api.import_for_summary(page, url, filename)
            except AttachmentTooLarge:
                import sys
                print(f'Large {block["type"]} retained as a source link: {ident}', file=sys.stderr)
                replacement = source_link(f'Open {block["type"]}: {filename} (over 20 MB)', source_url)
                block.clear()
                block.update(replacement)
                continue
            for _ in range(90):
                if upload['status'] == 'uploaded':
                    break
                if upload['status'] in ('failed', 'expired'):
                    raise RuntimeError('Media import failed for source block ' + ident)
                time.sleep(1)
                upload = api.call('GET', 'file_uploads/' + upload['id'])
            else:
                raise RuntimeError('Media import timed out for source block ' + ident)
            payload.update({'type': 'file_upload', 'file_upload': {'id': upload['id']}})
        materialize_media(api, page, payload.get('children', []))
