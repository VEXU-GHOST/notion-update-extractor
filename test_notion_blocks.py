import unittest
import notion_blocks as b

class NativeCopyTests(unittest.TestCase):
    def test_long_text_preserves_content_and_annotations(self):
        source=[{'type':'text','text':{'content':'x'*7083},'annotations':{'bold':True}}]
        result=b.clean_rich(source)
        self.assertEqual(''.join(r['text']['content'] for r in result),'x'*7083)
        self.assertTrue(all(len(r['text']['content'])<=2000 and r['annotations']['bold'] for r in result))
    def test_source_links_normalized_without_local_file_links(self):
        result=b.clean_rich([{'type':'text','text':{'content':'Page','link':{'url':'/p/abc'}}},{'type':'text','text':{'content':'Local file','link':{'url':'%3C/home/test.txt%3E'}}}])
        self.assertEqual(result[0]['text']['link']['url'],'https://www.notion.so/abc')
        self.assertNotIn('link',result[1]['text'])
        self.assertEqual(result[1]['text']['content'],'Local file')
    def test_preview_mentions_become_linked_text(self):
        for kind in ('link_mention','link_preview'):
            source={'type':'mention','plain_text':'Repo','mention':{'type':kind,kind:{'url':'https://github.com/test/repo'}}}
            result=b.clean_rich([source])[0]
            self.assertEqual(result['type'],'text')
            self.assertEqual(result['text']['link']['url'],'https://github.com/test/repo')
    def test_numbered_lists_remove_read_only_metadata(self):
        source={'id':'a','type':'numbered_list_item','numbered_list_item':{'rich_text':[],'list_format':'numbers','color':'default'}}
        result=b.clone(source,'https://www.notion.so/task')
        self.assertNotIn('list_format',result['numbered_list_item'])
        self.assertEqual(source['numbered_list_item']['list_format'],'numbers')
    def test_flattened_descendants_copy_once(self):
        child={'id':'b','type':'paragraph','paragraph':{'rich_text':[]}}
        parent={'id':'a','type':'bulleted_list_item','bulleted_list_item':{'rich_text':[]},'_children':[child]}
        result=b.clone_roots([parent,child], 'https://www.notion.so/task')
        self.assertEqual(len(result),1)
        self.assertEqual(len(result[0]['bulleted_list_item']['children']),1)
    def test_media_all_retained_without_expiring_urls(self):
        for kind in ('image','video','audio','pdf','file'):
            source={'id':'media','type':kind,kind:{'type':'file','file':{'url':'https://signed.example/expiring'},'caption':[]}}
            result=b.clone(source,'https://www.notion.so/task')
            self.assertEqual(result['type'],kind)
            self.assertEqual(result[kind]['_source_media_block'],'media')
            self.assertNotIn('file',result[kind])
    def test_only_buttons_are_dropped(self):
        self.assertIsNone(b.clone({'id':'x','type':'button','button':{}}, 'https://www.notion.so/task'))
        self.assertIsNone(b.clone({'id':'x','type':'unsupported','unsupported':{'block_type':'button'}}, 'https://www.notion.so/task'))
        unknown=b.clone({'id':'x','type':'unsupported','unsupported':{'block_type':'drive'}}, 'https://www.notion.so/task')
        self.assertEqual(unknown['paragraph']['rich_text'][0]['text']['link']['url'], 'https://www.notion.so/task#x')
    def test_large_media_retains_source_link(self):
        from unittest.mock import Mock
        from notion_feed import AttachmentTooLarge
        api=Mock()
        api.call.return_value={'video':{'file':{'url':'https://example.com/video.mp4'}}}
        api.import_for_summary.side_effect=AttachmentTooLarge('over limit')
        blocks=[b.clone({'id':'v','type':'video','video':{'type':'file','file':{'url':'old'}}},'https://www.notion.so/task')]
        b.materialize_media(api,'output',blocks)
        self.assertEqual(blocks[0]['type'],'paragraph')
        self.assertEqual(blocks[0]['paragraph']['rich_text'][0]['text']['link']['url'],'https://www.notion.so/task#v')
    def test_synced_block_with_button_copies_filtered_children(self):
        source={'id':'s','type':'synced_block','synced_block':{'synced_from':None},'_children':[
            {'id':'b','type':'unsupported','unsupported':{'block_type':'button'}},
            {'id':'p','type':'paragraph','paragraph':{'rich_text':[]}}]}
        result=b.clone(source,'https://www.notion.so/task')
        self.assertEqual(result['type'],'paragraph')
        self.assertEqual(len(result['paragraph']['children']),1)

if __name__=='__main__':unittest.main()
